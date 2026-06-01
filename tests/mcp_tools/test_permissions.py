"""
Unit tests for app/mcp/permissions.py.

Pure predicate tests over fabricated ResolvedIdentity values — no DB.
"""

import uuid

import pytest

from app.mcp.permissions import (
    ANY_AUTHENTICATED,
    CAN_CONTRIBUTE_WIKI,
    CAN_CREATE_WIKI_DIRECT,
    CAN_REVIEW_WIKI,
    REQUIRES_ATTR,
    ToolRequirement,
    kb_tool,
    requirement_for,
)
from app.services.mcp_auth_service import ResolvedIdentity


def _identity(
    *,
    is_admin: bool = False,
    permissions: list[str] | None = None,
) -> ResolvedIdentity:
    return ResolvedIdentity(
        employee_id=uuid.uuid4(),
        employee_name="Test User",
        department_ids=[uuid.uuid4()],
        department_names=["Test Dept"],
        is_admin=is_admin,
        permissions=permissions or [],
    )


# ---------------------------------------------------------------------------
# ANY_AUTHENTICATED
# ---------------------------------------------------------------------------

def test_any_authenticated_admits_admin():
    assert ANY_AUTHENTICATED.allows(_identity(is_admin=True))


def test_any_authenticated_admits_bare_employee():
    assert ANY_AUTHENTICATED.allows(_identity())


# ---------------------------------------------------------------------------
# CAN_CONTRIBUTE_WIKI
# ---------------------------------------------------------------------------

def test_contribute_admits_admin():
    assert CAN_CONTRIBUTE_WIKI.allows(_identity(is_admin=True))


def test_contribute_admits_wiki_write_own_dept():
    assert CAN_CONTRIBUTE_WIKI.allows(
        _identity(permissions=["wiki:write:own_dept"])
    )


def test_contribute_admits_wiki_write_all():
    assert CAN_CONTRIBUTE_WIKI.allows(
        _identity(permissions=["wiki:write:all"])
    )


def test_contribute_rejects_read_only_employee():
    assert not CAN_CONTRIBUTE_WIKI.allows(
        _identity(permissions=["wiki:read:own_dept", "doc:read:own_dept"])
    )


# ---------------------------------------------------------------------------
# CAN_REVIEW_WIKI
# ---------------------------------------------------------------------------

def test_review_admits_admin():
    assert CAN_REVIEW_WIKI.allows(_identity(is_admin=True))


def test_review_admits_wiki_write_all():
    assert CAN_REVIEW_WIKI.allows(
        _identity(permissions=["wiki:write:all"])
    )


def test_review_rejects_wiki_write_own_dept():
    # own_dept is contributor-tier in the org realm, not reviewer-tier.
    assert not CAN_REVIEW_WIKI.allows(
        _identity(permissions=["wiki:write:own_dept"])
    )





# ---------------------------------------------------------------------------
# CAN_CREATE_WIKI_DIRECT is the reviewer ladder
# ---------------------------------------------------------------------------

def test_create_direct_is_alias_of_review():
    # If we ever decouple them, this test fails and we update both call sites
    # in app/mcp/tools.py deliberately.
    assert CAN_CREATE_WIKI_DIRECT is CAN_REVIEW_WIKI


# ---------------------------------------------------------------------------
# Identity helpers
# ---------------------------------------------------------------------------

def test_has_permission_checks_membership():
    i = _identity(permissions=["wiki:write:all", "doc:read:all"])
    assert i.has_permission("wiki:write:all")
    assert not i.has_permission("wiki:write:own_dept")


def test_has_any_permission():
    i = _identity(permissions=["wiki:write:own_dept"])
    assert i.has_any_permission("wiki:write:own_dept", "wiki:write:all")
    assert not i.has_any_permission("doc:delete:all")





# ---------------------------------------------------------------------------
# kb_tool / requirement_for plumbing
# ---------------------------------------------------------------------------

class _FakeMCP:
    """Stand-in for FastMCP so we can exercise kb_tool without spinning up a server."""
    def __init__(self):
        self.registered: list = []

    def tool(self, **kwargs):
        def deco(fn):
            self.registered.append(fn)
            return fn
        return deco


def test_kb_tool_attaches_requirement_attr():
    fake = _FakeMCP()
    custom = ToolRequirement(predicate=lambda i: True, label="x")

    @kb_tool(fake, requires=custom)
    async def my_tool(): ...

    assert getattr(my_tool, REQUIRES_ATTR) is custom
    assert fake.registered == [my_tool]


def test_kb_tool_default_requirement_is_any_authenticated():
    fake = _FakeMCP()

    @kb_tool(fake)
    async def my_tool(): ...

    assert getattr(my_tool, REQUIRES_ATTR) is ANY_AUTHENTICATED


def test_requirement_for_falls_back_to_any_authenticated():
    async def bare(): ...
    assert requirement_for(bare) is ANY_AUTHENTICATED
