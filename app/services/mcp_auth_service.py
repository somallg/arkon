"""
MCP Auth Service — token verification and scope resolution.

This service is called by the MCP server to:
1. Verify employee MCP token
2. Resolve the employee's department
3. Compute the effective knowledge scope (what docs they can access)
4. Generate and revoke tokens

Permission model v2: uses source_departments M2M and scoped permissions.
"""

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger
from sqlalchemy import exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import (
    Employee,
    EmployeeDepartment,
    Source,
    SourceDepartment,
)

# `last_connected` is bumped on every authenticated MCP request — debounce so a
# single user spamming tool calls doesn't generate a write per call.
LAST_CONNECTED_DEBOUNCE = timedelta(seconds=60)


def hash_token(token: str) -> str:
    """HMAC-SHA256(pepper, token), hex-encoded.

    Tokens are 256-bit URL-safe random strings, so a plain HMAC (no bcrypt) is
    sufficient — there is no practical brute-force surface. The pepper means a
    raw DB dump alone is not enough to forge tokens.
    """
    return hmac.new(
        settings.mcp_token_pepper.encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


@dataclass
class ResolvedIdentity:
    """The authenticated employee context, passed to MCP tools."""
    employee_id: uuid.UUID
    employee_name: str
    # All departments the employee belongs to. May be empty — in which case the
    # user can only see resources scoped to 'global'. Replaces the legacy single
    # `department_id`; treat as an unordered set.
    department_ids: list[uuid.UUID] = field(default_factory=list)
    department_names: list[str] = field(default_factory=list)
    allowed_knowledge_types: Optional[list[str]] = None  # None = all
    allowed_source_ids: Optional[list[str]] = None       # None = all
    is_admin: bool = False
    permissions: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    # Predicate helpers — used by MCP tool-visibility middleware so the   #
    # list_tools hook stays I/O-free.                                     #
    # ------------------------------------------------------------------ #

    def has_permission(self, perm: str) -> bool:
        """True if `perm` is in the identity's resolved permission set."""
        return perm in self.permissions

    def has_any_permission(self, *perms: str) -> bool:
        """True if any of `perms` is present."""
        return any(p in self.permissions for p in perms)


class MCPAuthService:
    """Handles MCP token auth and knowledge scope resolution."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def verify_token(self, token: str) -> Optional[ResolvedIdentity]:
        """
        Verify an MCP bearer token and return the resolved identity.
        Returns None if token is invalid/inactive.

        Sets `identity._last_connected_bumped` (transient flag on the returned
        identity, accessed via `bumped_last_connected` below) so callers know
        whether a commit is needed.
        """
        token_hash = hash_token(token)
        stmt = (
            select(Employee)
            .where(
                Employee.mcp_token_hash == token_hash,
                Employee.is_active.is_(True),
            )
            .options(
                selectinload(Employee.employee_departments).selectinload(
                    EmployeeDepartment.department
                ),
            )
        )
        result = await self.db.execute(stmt)
        employee = result.scalar_one_or_none()

        if not employee:
            return None

        # Debounce last_connected — at most one write per minute per employee.
        now = datetime.now(timezone.utc)
        prev = employee.last_connected
        self._bumped = False
        if prev is None or (now - prev) > LAST_CONNECTED_DEBOUNCE:
            employee.last_connected = now
            await self.db.flush()
            self._bumped = True

        # Resolve knowledge scope
        identity = await self._resolve_scope(employee)
        return identity

    @property
    def bumped_last_connected(self) -> bool:
        """True if the most recent verify_token actually wrote last_connected."""
        return getattr(self, "_bumped", False)

    async def _resolve_scope(self, employee: Employee) -> ResolvedIdentity:
        """
        Compute effective knowledge scope for an employee.
        Uses new permission model v2 (scoped permissions + source_departments).
        """
        from app.services.permission_engine import (
            get_effective_permissions,
            get_scope_level,
        )

        permissions = get_effective_permissions(employee)

        department_ids = [ed.department_id for ed in employee.employee_departments]
        department_names = [
            ed.department.name for ed in employee.employee_departments if ed.department
        ]

        if employee.role == "admin":
            return ResolvedIdentity(
                employee_id=employee.id,
                employee_name=employee.name,
                department_ids=department_ids,
                department_names=department_names,
                is_admin=True,
                permissions=permissions,
            )

        # Determine doc:read scope
        scope = get_scope_level(permissions, "doc", "read")

        if scope == "all":
            # Can read all documents
            return ResolvedIdentity(
                employee_id=employee.id,
                employee_name=employee.name,
                department_ids=department_ids,
                department_names=department_names,
                permissions=permissions,
            )

        if scope == "own_dept":
            # Can read global docs + docs in any of the user's departments.
            allowed_ids = await self._get_department_source_ids(department_ids)
            return ResolvedIdentity(
                employee_id=employee.id,
                employee_name=employee.name,
                department_ids=department_ids,
                department_names=department_names,
                allowed_source_ids=allowed_ids,
                permissions=permissions,
            )

        # No doc:read permission at all
        return ResolvedIdentity(
            employee_id=employee.id,
            employee_name=employee.name,
            department_ids=department_ids,
            department_names=department_names,
            allowed_source_ids=[],  # empty = no access
            permissions=permissions,
        )

    async def _get_department_source_ids(
        self, department_ids: list[uuid.UUID],
    ) -> list[str]:
        """Get IDs of sources that are global (no departments) or in any of the
        given departments. Empty `department_ids` returns only global sources.
        """
        # Sources with no department entries (global)
        global_stmt = (
            select(Source.id)
            .where(
                ~exists(
                    select(SourceDepartment.source_id)
                    .where(SourceDepartment.source_id == Source.id)
                )
            )
        )
        global_result = await self.db.execute(global_stmt)
        global_ids = [str(r[0]) for r in global_result.all()]

        if not department_ids:
            return global_ids

        # Sources in any of these departments
        dept_stmt = (
            select(SourceDepartment.source_id)
            .where(SourceDepartment.department_id.in_(department_ids))
            .distinct()
        )
        dept_result = await self.db.execute(dept_stmt)
        dept_ids = [str(r[0]) for r in dept_result.all()]

        return global_ids + dept_ids

    # --- Token Management ---

    async def generate_token(self, employee_id: uuid.UUID) -> str:
        """Generate a new MCP token for an employee.

        Returns the plaintext token to the caller exactly once — only the hash
        is persisted. There is no read-back path; if the user loses it they
        must rotate again.
        """
        token = f"ark_{secrets.token_urlsafe(32)}"

        stmt = (
            update(Employee)
            .where(Employee.id == employee_id)
            .values(
                mcp_token=None,  # ensure legacy plaintext stays cleared
                mcp_token_hash=hash_token(token),
                mcp_token_prefix=token[:12],
                mcp_token_rotated_at=datetime.now(timezone.utc),
            )
        )
        await self.db.execute(stmt)
        await self.db.flush()

        logger.info(f"Generated MCP token for employee {employee_id}")
        return token

    async def revoke_token(self, employee_id: uuid.UUID) -> bool:
        """Revoke an employee's MCP token."""
        stmt = (
            update(Employee)
            .where(Employee.id == employee_id)
            .values(
                mcp_token=None,
                mcp_token_hash=None,
                mcp_token_prefix=None,
            )
        )
        result = await self.db.execute(stmt)
        await self.db.flush()
        return (result.rowcount or 0) > 0  # type: ignore[union-attr]


def apply_scope_filter(query, identity: ResolvedIdentity):
    """
    Apply knowledge scope filters to a SQLAlchemy query on the Source table.

    Sources are accessible when any of these conditions is true:
      1. No scope restrictions defined (open access)
      2. Source ID is in allowed_source_ids (explicit grant)
      3. Source knowledge_type is in allowed_knowledge_types (type-based grant)

    Usage:
        stmt = select(Source).where(Source.status == "ready")
        stmt = apply_scope_filter(stmt, identity)
    """
    if identity.allowed_source_ids is None and identity.allowed_knowledge_types is None:
        # Open access
        return query

    conditions = []

    if identity.allowed_source_ids is not None:
        conditions.append(Source.id.in_([uuid.UUID(s) for s in identity.allowed_source_ids]))

    if identity.allowed_knowledge_types is not None:
        from sqlalchemy import select as sa_select

        from app.database.models import KnowledgeType
        kt_subq = sa_select(KnowledgeType.id).where(
            KnowledgeType.slug.in_(identity.allowed_knowledge_types)
        )
        conditions.append(Source.knowledge_type_id.in_(kt_subq))

    if conditions:
        query = query.where(or_(*conditions))

    return query
