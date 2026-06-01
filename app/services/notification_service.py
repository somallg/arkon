"""
Notification Service — in-app notification inbox.

Sync DB writes (no enqueue) following the audit_service pattern: callers add
notifications and commit themselves. ContributionService is the primary
producer; the routers and frontend consume via /notifications.

Notification types are dotted strings (`<artifact>.<event>`) so the frontend
can route to the right UI without coupling to backend enums.
"""

import uuid
from contextvars import ContextVar
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    Employee,
    Notification,
)


class NotificationType:
    """String constants for notification.type — keep stable; frontend matches."""

    WIKI_DRAFT_SUBMITTED = "wiki_draft.submitted"
    WIKI_DRAFT_RESUBMITTED = "wiki_draft.resubmitted"
    WIKI_DRAFT_APPROVED = "wiki_draft.approved"
    WIKI_DRAFT_REJECTED = "wiki_draft.rejected"
    WIKI_DRAFT_CHANGES_REQUESTED = "wiki_draft.changes_requested"
    WIKI_DRAFT_WITHDRAWN = "wiki_draft.withdrawn"

    SKILL_CONTRIBUTION_SUBMITTED = "skill_contribution.submitted"
    SKILL_CONTRIBUTION_RESUBMITTED = "skill_contribution.resubmitted"
    SKILL_CONTRIBUTION_APPROVED = "skill_contribution.approved"
    SKILL_CONTRIBUTION_REJECTED = "skill_contribution.rejected"
    SKILL_CONTRIBUTION_CHANGES_REQUESTED = "skill_contribution.changes_requested"
    SKILL_CONTRIBUTION_WITHDRAWN = "skill_contribution.withdrawn"


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

async def notify(
    db: AsyncSession,
    recipient_id: uuid.UUID,
    type: str,
    subject: str,
    target_type: str,
    target_id: str,
    body: str = "",
    actor_id: Optional[uuid.UUID] = None,
) -> Notification:
    """Insert one notification. Caller must commit."""
    n = Notification(
        recipient_id=recipient_id,
        type=type,
        subject=subject,
        body=body,
        target_type=target_type,
        target_id=str(target_id),
        actor_id=actor_id,
    )
    db.add(n)
    _stage_for_dispatch(db, [n])
    return n


async def notify_each(
    db: AsyncSession,
    items: list[dict],
    type: str,
    target_type: str,
    actor_id: Optional[uuid.UUID] = None,
    exclude_actor: bool = True,
) -> list[Notification]:
    """Insert one notification per item — each item has its own subject / body /
    target_id. Used when recipients need distinct payloads (e.g. sibling-draft
    notifications that link back to each author's own draft).

    Item shape: {"recipient_id": UUID, "subject": str, "body": str, "target_id": str}

    Single batched INSERT via SQLAlchemy `add_all` — much cheaper than calling
    `notify()` in a loop for large fan-outs.
    """
    out: list[Notification] = []
    seen: set[uuid.UUID] = set()
    for item in items:
        rid = item.get("recipient_id")
        if rid is None or rid in seen:
            continue
        if exclude_actor and actor_id is not None and rid == actor_id:
            continue
        seen.add(rid)
        out.append(Notification(
            recipient_id=rid,
            type=type,
            subject=item.get("subject", ""),
            body=item.get("body", ""),
            target_type=target_type,
            target_id=str(item.get("target_id") or ""),
            actor_id=actor_id,
        ))
    if not out:
        return out
    db.add_all(out)
    _stage_for_dispatch(db, out)
    return out


async def notify_many(
    db: AsyncSession,
    recipient_ids: Iterable[uuid.UUID],
    type: str,
    subject: str,
    target_type: str,
    target_id: str,
    body: str = "",
    actor_id: Optional[uuid.UUID] = None,
    exclude_actor: bool = True,
) -> list[Notification]:
    """Insert one notification per recipient. Caller must commit.

    `exclude_actor=True` skips notifying the actor of their own action — the
    common case (don't tell a reviewer they approved a draft).
    """
    out: list[Notification] = []
    seen: set[uuid.UUID] = set()
    for rid in recipient_ids:
        if rid is None:
            continue
        if rid in seen:
            continue
        if exclude_actor and actor_id is not None and rid == actor_id:
            continue
        seen.add(rid)
        out.append(await notify(
            db, rid, type=type, subject=subject, target_type=target_type,
            target_id=target_id, body=body, actor_id=actor_id,
        ))
    # Each `notify` call already staged itself; no extra stage call here.
    return out


# ---------------------------------------------------------------------------
# Dispatch staging — accumulate notifications per request via a contextvar so
# the dispatcher middleware can fan them out to external channels (email,
# webhook) AFTER the caller commits. Falls back to a session-keyed dict when
# called outside a request scope (e.g. arq worker).
# ---------------------------------------------------------------------------

_REQUEST_STAGED: ContextVar[Optional[list[Notification]]] = ContextVar(
    "_notif_staged_request", default=None,
)
_SESSION_STAGED: dict[int, list[Notification]] = {}


def init_request_dispatch_scope() -> None:
    """Called by the dispatcher middleware at the start of every HTTP request."""
    _REQUEST_STAGED.set([])


def _stage_for_dispatch(db: AsyncSession, notifs: list[Notification]) -> None:
    if not notifs:
        return
    bucket = _REQUEST_STAGED.get()
    if bucket is not None:
        bucket.extend(notifs)
    else:
        _SESSION_STAGED.setdefault(id(db), []).extend(notifs)


def take_pending_dispatch(db: Optional[AsyncSession] = None) -> list[Notification]:
    """Pop the staged notifications for the current request / session."""
    bucket = _REQUEST_STAGED.get()
    if bucket is not None:
        out = list(bucket)
        bucket.clear()
        return out
    if db is None:
        return []
    return _SESSION_STAGED.pop(id(db), [])


async def dispatch_pending(db: Optional[AsyncSession] = None) -> None:
    """Pop staged notifications and run external dispatch.

    Opens a fresh session for external lookups because the request's session
    may already be closed by the time middleware runs us.
    """
    staged = take_pending_dispatch(db)
    if not staged:
        return
    from app.database import async_session_factory
    from app.services.notification_dispatch import dispatch_external
    async with async_session_factory() as fresh:
        await dispatch_external(fresh, staged)


# ---------------------------------------------------------------------------
# Recipient resolution helpers
# ---------------------------------------------------------------------------

async def get_reviewers_for_scope(
    db: AsyncSession,
    scope_type: str,
    scope_id: Optional[uuid.UUID],
) -> list[uuid.UUID]:
    """Resolve the right reviewer set for a wiki page's scope (Admins & Knowledge Managers)."""
    from sqlalchemy import or_
    rows = await db.execute(
        select(Employee.id).where(
            or_(
                Employee.role == "admin",
                Employee.global_role == "admin",
                Employee.global_role == "knowledge_manager",
            )
        )
    )
    return [r[0] for r in rows.all()]
