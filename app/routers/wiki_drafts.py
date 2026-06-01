"""
Wiki Draft router — propose, review, approve, and reject wiki page drafts.

Permission model:
  - Propose (POST /drafts): workspace contributor+ OR global wiki:write
  - Review/Approve/Reject: workspace editor+ OR wiki:write:all OR admin
"""

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlalchemy import and_, or_, func, select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.database.models import (
    Employee,
    WikiDraftRound,
    WikiPage,
    WikiPageDraft,
)
from app.services import contribution_service, wiki_service
from app.services.audit_service import log_audit
from app.services.auth_service import get_current_user, require_permission
from app.services.contribution_service import (
    InvalidTransition,
    wiki_draft_adapter,
)
from app.services.permission_engine import (
    _get_user_permissions,
    has_any_permission,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ProposeDraftRequest(BaseModel):
    content_md: str
    note: Optional[str] = None
    base_version: Optional[int] = None
    scope_type: Optional[str] = None
    scope_id: Optional[uuid.UUID] = None
    branch_id: Optional[uuid.UUID] = None

    @field_validator("content_md")
    @classmethod
    def not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("content_md must not be empty")
        if len(v) > 50_000:
            raise ValueError("content_md exceeds 50,000 character limit")
        return v


class ProposeCreateRequest(BaseModel):
    slug: str
    title: str
    page_type: str = "concept"
    knowledge_type_slugs: list[str] = []
    scope_type: str = "global"
    scope_id: Optional[uuid.UUID] = None
    content_md: str
    summary: str = ""
    note: Optional[str] = None
    branch_id: Optional[uuid.UUID] = None

    @field_validator("content_md")
    @classmethod
    def content_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("content_md must not be empty")
        if len(v) > 50_000:
            raise ValueError("content_md exceeds 50,000 character limit")
        return v

    @field_validator("slug")
    @classmethod
    def slug_format(cls, v: str) -> str:
        v = v.strip()
        if not v or v in (wiki_service.INDEX_SLUG, wiki_service.LOG_SLUG):
            raise ValueError("slug must be non-empty and not reserved")
        if any(c.isspace() for c in v):
            raise ValueError("slug must not contain whitespace")
        return v

    @field_validator("page_type")
    @classmethod
    def page_type_known(cls, v: str) -> str:
        if v not in wiki_service.PAGE_TYPES:
            raise ValueError(f"page_type must be one of {sorted(wiki_service.PAGE_TYPES)}")
        return v

    @field_validator("scope_type")
    @classmethod
    def scope_known(cls, v: str) -> str:
        if v not in ("global", "department"):
            raise ValueError("scope_type must be global or department")
        return v


class BulkApproveRequest(BaseModel):
    draft_ids: list[uuid.UUID]
    allow_conflict: bool = False
    reviewer_note: Optional[str] = None  # applied to every draft


class BulkApproveItemResult(BaseModel):
    draft_id: uuid.UUID
    status: str  # "approved" | "skipped" | "error"
    message: Optional[str] = None
    page_version: Optional[int] = None


class BulkApproveResponse(BaseModel):
    results: list[BulkApproveItemResult]
    approved: int
    skipped: int
    errored: int


class ApproveDraftRequest(BaseModel):
    reviewer_note: Optional[str] = None
    edited_content_md: Optional[str] = None
    allow_conflict: bool = False
    # When approving a draft_kind='create' draft, the reviewer can override
    # the contributor's suggested metadata before materialising the page.
    final_slug: Optional[str] = None
    final_title: Optional[str] = None
    final_page_type: Optional[str] = None
    final_knowledge_type_slugs: Optional[list[str]] = None


class RejectDraftRequest(BaseModel):
    reviewer_note: str

    @field_validator("reviewer_note")
    @classmethod
    def not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("reviewer_note is required when rejecting")
        return v


class RequestChangesRequest(BaseModel):
    reviewer_note: str

    @field_validator("reviewer_note")
    @classmethod
    def not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("reviewer_note is required when requesting changes")
        return v


class ResubmitDraftRequest(BaseModel):
    content_md: str
    note: Optional[str] = None

    @field_validator("content_md")
    @classmethod
    def not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("content_md must not be empty")
        if len(v) > 50_000:
            raise ValueError("content_md exceeds 50,000 character limit")
        return v


class DraftRoundResponse(BaseModel):
    id: uuid.UUID
    round_no: int
    content_md: str
    author_note: Optional[str]
    reviewer_return_note: Optional[str]
    ai_check_results: Optional[dict] = None
    submitted_at: str


class AuthorStats(BaseModel):
    """Lightweight author reputation surfaced alongside each draft."""
    approved: int = 0
    rejected: int = 0
    needs_revision: int = 0
    total_reviewed: int = 0
    accuracy: float = 0.0  # approved / (approved + rejected); 0..1


class SuggestedReviewer(BaseModel):
    """Reviewer the system would route this draft to based on past activity."""
    id: uuid.UUID
    name: Optional[str] = None
    email: Optional[str] = None
    score: int  # number of past approvals on overlapping pages


class DraftResponse(BaseModel):
    id: uuid.UUID
    page_id: Optional[uuid.UUID] = None
    page_slug: str
    page_title: str
    page_scope_type: str = "global"
    page_scope_id: Optional[uuid.UUID] = None
    page_scope_name: Optional[str] = None
    page_version: int
    base_version: Optional[int] = None
    has_conflict: bool = False
    draft_kind: str = "edit"
    suggested_metadata: Optional[dict] = None
    author_id: Optional[uuid.UUID]
    author_name: Optional[str]
    author_stats: Optional[AuthorStats] = None
    suggested_reviewers: list[SuggestedReviewer] = []
    content_md: str
    note: Optional[str]
    status: str
    revision_round: int = 0
    last_returned_note: Optional[str] = None
    ai_check_status: str = "pending"
    ai_check_results: Optional[dict] = None
    ai_checked_at: Optional[str] = None
    source: str
    reviewed_by_name: Optional[str] = None
    reviewed_at: Optional[str] = None
    reviewer_note: Optional[str] = None
    created_at: str
    updated_at: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _can_propose(db: AsyncSession, user: Employee, page: WikiPage) -> bool:
    """Permission to propose an edit on `page`.

    - Department pages: `wiki:write:all` for any dept, or
      `wiki:write:own_dept` ONLY when the page belongs to the user's own
      department.
    - Global pages: any wiki:write permission.
    """
    if user.role == "admin":
        return True
    perms = _get_user_permissions(user)
    if page.scope_type == "department" and page.scope_id:
        if "wiki:write:all" in perms:
            return True
        if "wiki:write:own_dept" in perms and page.scope_id in user.department_ids:
            return True
        return False
    return has_any_permission(list(perms), "wiki", "write")


async def _can_review(db: AsyncSession, user: Employee, page: WikiPage) -> bool:
    """wiki:write:all, or admin."""
    if user.role == "admin":
        return True
    perms = _get_user_permissions(user)
    return "wiki:write:all" in perms


async def _can_review_scope(
    db: AsyncSession,
    user: Employee,
    scope_type: str,
    scope_id: Optional[uuid.UUID],
) -> bool:
    """Reviewer check for a (scope_type, scope_id) pair."""
    if user.role == "admin":
        return True
    perms = _get_user_permissions(user)
    return "wiki:write:all" in perms


async def _can_review_draft(
    db: AsyncSession, user: Employee, draft: WikiPageDraft,
) -> bool:
    """Reviewer check that handles both edit and create drafts uniformly."""
    if draft.draft_kind == "create":
        sm = draft.suggested_metadata or {}
        scope_type = sm.get("scope_type") or "global"
        scope_id_raw = sm.get("scope_id")
        try:
            scope_id = uuid.UUID(scope_id_raw) if isinstance(scope_id_raw, str) else scope_id_raw
        except (ValueError, TypeError):
            scope_id = None
        if scope_id is not None and not isinstance(scope_id, uuid.UUID):
            # Defensive: non-string non-UUID values (e.g. ints) leak through if
            # suggested_metadata was hand-crafted. Treat as missing rather than
            # passing a junk type down to get_workspace_role.
            scope_id = None
        return await _can_review_scope(db, user, scope_type, scope_id)
    # Edit drafts: defer to page-based check.
    page = await db.get(WikiPage, draft.page_id) if draft.page_id else None
    if not page:
        return user.role == "admin"
    return await _can_review(db, user, page)


def _build_reviewable_page_filter(user: Employee):
    """SQL filter selecting WikiPage rows the user can review (editor+).

    Returns None if the user can review everything (admin / wiki:write:all),
    otherwise a falsy filter (False) since there are no more project scopes.
    """
    if user.role == "admin":
        return None
    perms = _get_user_permissions(user)
    can_global = "wiki:write:all" in perms
    if can_global:
        return None

    # No more projects, so non-global-reviewers can review nothing
    return False


def _expire_after_failed_approve(
    db: AsyncSession, draft: WikiPageDraft, page: Optional[WikiPage],
) -> None:
    """Expire ORM attributes touched by a partial approve so a savepoint
    rollback doesn't leave stale (e.g. bumped version) values on the
    identity-mapped objects. Safe to call with `page=None`."""
    try:
        db.expire(draft)
        if page is not None:
            db.expire(page)
    except Exception:
        # If the object is no longer in the session, nothing to expire.
        pass


async def _load_draft(db: AsyncSession, draft_id: str) -> WikiPageDraft:
    try:
        did = uuid.UUID(draft_id)
    except ValueError:
        raise HTTPException(400, "Invalid draft ID format")
    draft = await db.get(WikiPageDraft, did)
    if not draft:
        raise HTTPException(404, f"Draft {draft_id} not found")
    return draft


async def _suggested_reviewers(
    db: AsyncSession,
    draft: WikiPageDraft,
    limit: int = 3,
) -> list[SuggestedReviewer]:
    """Rank candidate reviewers by past activity on overlapping pages.

    Signal: count `wiki_page_revisions` rows (change_type in editor_edit /
    draft_approved / draft_approved_create) where the page either:
    - shares at least one knowledge_type_slug with this draft's page, OR
    - belongs to the same scope (project/department/global).

    Falls back to recent global reviewers if no overlap exists. Excludes
    the draft's own author. Top `limit` returned with their approval count.
    """
    from app.database.models import WikiPageRevision

    # Resolve the page metadata we'll use to define "overlap".
    page = await db.get(WikiPage, draft.page_id) if draft.page_id else None
    if page is None:
        # Create draft: use suggested metadata.
        sm = draft.suggested_metadata or {}
        kt_slugs = sm.get("knowledge_type_slugs") or []
        scope_type = sm.get("scope_type") or "global"
        scope_id_raw = sm.get("scope_id")
        try:
            scope_id = uuid.UUID(scope_id_raw) if isinstance(scope_id_raw, str) else scope_id_raw
        except (ValueError, TypeError):
            scope_id = None
    else:
        kt_slugs = page.knowledge_type_slugs or []
        scope_type = page.scope_type or "global"
        scope_id = page.scope_id

    # Build the OR-filter that defines "similar pages".
    from sqlalchemy import or_
    clauses = []
    if kt_slugs:
        clauses.append(WikiPage.knowledge_type_slugs.overlap(kt_slugs))  # type: ignore[arg-type]
    clauses.append(
        and_(WikiPage.scope_type == scope_type, WikiPage.scope_id == scope_id)
        if scope_id is not None
        else and_(WikiPage.scope_type == scope_type, WikiPage.scope_id.is_(None))
    )
    similar_pages_stmt = select(WikiPage.id).where(or_(*clauses))

    # Rank by approval count across those pages.
    rows = (await db.execute(
        select(
            WikiPageRevision.changed_by_id,
            func.count(WikiPageRevision.id).label("cnt"),
        )
        .where(
            WikiPageRevision.changed_by_id.is_not(None),
            WikiPageRevision.change_type.in_(
                ["editor_edit", "draft_approved", "draft_approved_create", "rollback"]
            ),
            WikiPageRevision.page_id.in_(similar_pages_stmt),
        )
        .group_by(WikiPageRevision.changed_by_id)
        .order_by(func.count(WikiPageRevision.id).desc())
        .limit(limit + 2)  # +2 so we can drop author + self if needed
    )).all()

    out: list[SuggestedReviewer] = []
    for row in rows:
        reviewer_id, count = row[0], int(row[1])
        if draft.author_id is not None and reviewer_id == draft.author_id:
            continue  # author can't review own draft
        emp = await db.get(Employee, reviewer_id)
        if not emp:
            continue
        out.append(SuggestedReviewer(
            id=emp.id, name=emp.name, email=emp.email, score=count,
        ))
        if len(out) >= limit:
            break
    return out


async def _author_stats(db: AsyncSession, author_id: Optional[uuid.UUID]) -> Optional[AuthorStats]:
    """Count this author's historical drafts grouped by terminal status.

    Returns None for anonymous / unknown authors. Cheap query — one
    aggregated SELECT against wiki_page_drafts indexed by author_id.
    """
    if not author_id:
        return None
    rows = (await db.execute(
        select(WikiPageDraft.status, func.count(WikiPageDraft.id))
        .where(WikiPageDraft.author_id == author_id)
        .group_by(WikiPageDraft.status)
    )).all()
    counts = {row[0]: int(row[1]) for row in rows}
    approved = counts.get("approved", 0)
    rejected = counts.get("rejected", 0)
    needs_revision = counts.get("needs_revision", 0)
    total = approved + rejected
    accuracy = (approved / total) if total > 0 else 0.0
    return AuthorStats(
        approved=approved,
        rejected=rejected,
        needs_revision=needs_revision,
        total_reviewed=total,
        accuracy=round(accuracy, 3),
    )


async def _draft_response(db: AsyncSession, draft: WikiPageDraft) -> DraftResponse:
    page = await db.get(WikiPage, draft.page_id) if draft.page_id else None
    author = await db.get(Employee, draft.author_id) if draft.author_id else None
    reviewer = await db.get(Employee, draft.reviewed_by_id) if draft.reviewed_by_id else None
    current_version = page.version if page else 1
    has_conflict = bool(
        draft.status == "pending"
        and draft.base_version is not None
        and current_version is not None
        and draft.base_version < current_version
    )
    # Display slug/title come from the existing page for edit drafts, or
    # from the contributor's suggested metadata for create drafts. Scope is
    # also surfaced explicitly so the frontend can build correct deep links
    # even when the same slug exists in multiple scopes.
    suggested = draft.suggested_metadata or {}
    display_slug = (page.slug if page else suggested.get("slug")) or ""
    display_title = (page.title if page else suggested.get("title")) or ""
    if page is not None:
        page_scope_type = page.scope_type or "global"
        page_scope_id = page.scope_id
    else:
        page_scope_type = suggested.get("scope_type") or "global"
        sid_raw = suggested.get("scope_id")
        try:
            page_scope_id = uuid.UUID(sid_raw) if isinstance(sid_raw, str) else sid_raw
        except (ValueError, TypeError):
            page_scope_id = None

    # Resolve a human label for the scope so the queue can render e.g. "IT"
    # next to a slug rather than the raw UUID.
    page_scope_name: Optional[str] = None
    if page_scope_id is not None:
        if page_scope_type == "department":
            from app.database.models import Department
            d = await db.get(Department, page_scope_id)
            page_scope_name = d.name if d else None
    return DraftResponse(
        id=draft.id,
        page_id=draft.page_id,
        page_slug=display_slug,
        page_title=display_title,
        page_scope_type=page_scope_type,
        page_scope_id=page_scope_id,
        page_scope_name=page_scope_name,
        page_version=current_version or 1,
        base_version=draft.base_version,
        has_conflict=has_conflict,
        draft_kind=draft.draft_kind or "edit",
        suggested_metadata=suggested or None,
        author_id=draft.author_id,
        author_name=author.name if author else None,
        author_stats=await _author_stats(db, draft.author_id),
        suggested_reviewers=(
            await _suggested_reviewers(db, draft)
            if draft.status in ("pending", "needs_revision")
            else []
        ),
        content_md=draft.content_md,
        note=draft.note,
        status=draft.status,
        revision_round=draft.revision_round or 0,
        last_returned_note=draft.last_returned_note,
        ai_check_status=draft.ai_check_status or "pending",
        ai_check_results=draft.ai_check_results,
        ai_checked_at=draft.ai_checked_at.isoformat() if draft.ai_checked_at else None,
        source=draft.source,
        reviewed_by_name=reviewer.name if reviewer else None,
        reviewed_at=draft.reviewed_at.isoformat() if draft.reviewed_at else None,
        reviewer_note=draft.reviewer_note,
        created_at=draft.created_at.isoformat(),
        updated_at=draft.updated_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/wiki/pages/{slug:path}/drafts", response_model=DraftResponse, status_code=201)
async def propose_draft(
    slug: str,
    body: ProposeDraftRequest,
    db: AsyncSession = Depends(get_db),
    user: Employee = Depends(get_current_user),
):
    """Propose an edit to an existing wiki page. Creates a pending draft for editor review."""
    if slug in (wiki_service.INDEX_SLUG, wiki_service.LOG_SLUG):
        raise HTTPException(400, "Cannot propose drafts for reserved pages")

    if body.scope_type is not None:
        page = await wiki_service.get_page_by_slug(
            db, slug, scope_type=body.scope_type, scope_id=body.scope_id,
        )
    else:
        page = await wiki_service.get_page_by_slug_any_scope(db, slug)
    if not page:
        raise HTTPException(404, f"Wiki page not found: {slug}")

    if not await _can_propose(db, user, page):
        raise HTTPException(403, "Insufficient permission to propose a draft for this page")

    # If client passed base_version, sanity-check it matches a real prior
    # version (≤ current). If omitted, default to the page's current version
    # so future approvals can detect drift.
    base_version = body.base_version if body.base_version is not None else page.version
    if base_version is not None and page.version is not None and base_version > page.version:
        raise HTTPException(400, f"base_version {base_version} is ahead of current page v{page.version}")

    draft = await wiki_service.create_draft(
        db,
        page_id=page.id,
        author_id=user.id,
        content_md=body.content_md,
        note=body.note,
        source="web_ui",
        base_version=base_version,
    )
    # The lazy `draft.page` relationship won't be populated for the freshly
    # created row inside this session — set it manually so the adapter can
    # resolve the page scope without an extra round trip.
    draft.page = page
    if hasattr(body, "branch_id") and body.branch_id:
        draft.branch_id = body.branch_id
    await log_audit(db, user, "create", "wiki_draft", str(draft.id), reason=f"draft for: {slug}")
    await contribution_service.notify_submitted(db, wiki_draft_adapter, draft, user)
    await db.commit()
    await db.refresh(draft)
    return await _draft_response(db, draft)


@router.get("/wiki/drafts", response_model=list[DraftResponse])
async def list_all_drafts(
    status: Optional[str] = Query("pending", description="Filter by status: pending | approved | rejected | needs_revision | withdrawn"),
    mine: bool = Query(False, description="When true, list drafts authored by the current user instead of drafts to review"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: Employee = require_permission("wiki:read"),
):
    """List wiki drafts.

    Two modes:
    - default: drafts the current user can review (admins see everything;
      editors see their workspace + global; etc.).
    - `mine=true`: drafts the current user authored, regardless of scope.

    LEFT JOIN on WikiPage so create-kind drafts (page_id NULL) are visible.
    Permission filtering runs in SQL so pagination is correct — previously a
    post-query filter would silently drop rows past the first `limit`.
    """
    stmt = (
        select(WikiPageDraft)
        .outerjoin(WikiPage, WikiPage.id == WikiPageDraft.page_id)
        .options(
            selectinload(WikiPageDraft.page),
            selectinload(WikiPageDraft.author),
            selectinload(WikiPageDraft.reviewer),
        )
        .order_by(WikiPageDraft.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if status:
        stmt = stmt.where(WikiPageDraft.status == status)

    if mine:
        stmt = stmt.where(WikiPageDraft.author_id == user.id)
    else:
        # Exclude drafts that belong to unsubmitted branches
        from app.database.models import WikiBranch
        stmt = stmt.outerjoin(WikiBranch, WikiBranch.id == WikiPageDraft.branch_id)
        stmt = stmt.where(or_(WikiPageDraft.branch_id == None, WikiBranch.status == "pending_merge"))

        page_filter = _build_reviewable_page_filter(user)
        if page_filter is False:  # noqa: E712 — never true, kept for symmetry
            return []
        if page_filter is not None:
            stmt = stmt.where(page_filter)

    drafts = (await db.execute(stmt)).scalars().all()
    return [await _draft_response(db, d) for d in drafts]


@router.get("/wiki/pages/{slug:path}/drafts", response_model=list[DraftResponse])
async def list_page_drafts(
    slug: str,
    status: Optional[str] = Query(None),
    scope_type: Optional[str] = Query(None),
    scope_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    user: Employee = Depends(get_current_user),
):
    """List drafts for a specific wiki page.

    Reviewers see every draft on the page; authors always see their own
    drafts so they can withdraw / resubmit / inspect from the wiki UI.
    """
    sid = uuid.UUID(scope_id) if scope_id else None
    if scope_type:
        page = await wiki_service.get_page_by_slug(
            db, slug, scope_type=scope_type, scope_id=sid,
        )
    else:
        page = await wiki_service.get_page_by_slug_any_scope(db, slug)
    if not page:
        raise HTTPException(404, f"Wiki page not found: {slug}")

    is_reviewer = await _can_review(db, user, page)
    stmt = (
        select(WikiPageDraft)
        .where(WikiPageDraft.page_id == page.id)
        .order_by(WikiPageDraft.created_at.desc())
    )
    if not is_reviewer:
        # Author-only mode: restrict to drafts this user authored. No 403 here
        # so the wiki UI can hit this endpoint unconditionally and just get
        # back an empty list for users with neither role.
        stmt = stmt.where(WikiPageDraft.author_id == user.id)
    if status:
        stmt = stmt.where(WikiPageDraft.status == status)

    drafts = (await db.execute(stmt)).scalars().all()
    return [await _draft_response(db, d) for d in drafts]


@router.get("/wiki/drafts/{draft_id}", response_model=DraftResponse)
async def get_draft(
    draft_id: str,
    db: AsyncSession = Depends(get_db),
    user: Employee = Depends(get_current_user),
):
    """Get a single draft by ID. Author OR reviewer of the draft can read."""
    draft = await _load_draft(db, draft_id)
    if user.role != "admin" and draft.author_id != user.id:
        if not await _can_review_draft(db, user, draft):
            raise HTTPException(403, "Insufficient permission to view this draft")
    return await _draft_response(db, draft)


@router.post("/wiki/drafts/{draft_id}/approve", response_model=DraftResponse)
async def approve_draft(
    draft_id: str,
    body: ApproveDraftRequest,
    db: AsyncSession = Depends(get_db),
    user: Employee = Depends(get_current_user),
):
    """Approve a pending draft. Optionally provide edited content before approving.

    For draft_kind='create' the page is materialised at this point using
    `draft.suggested_metadata` (with optional reviewer overrides in the
    request body).
    """
    draft = await _load_draft(db, draft_id)
    if draft.status != "pending":
        raise HTTPException(400, f"Draft is already {draft.status}")

    if not await _can_review_draft(db, user, draft):
        raise HTTPException(403, "Insufficient permission to approve this draft")

    # Authors cannot approve their own drafts (admins exempt).
    if user.role != "admin" and draft.author_id == user.id:
        raise HTTPException(403, "You cannot approve your own draft. Ask another editor to review it.")

    metadata_overrides = None
    if draft.draft_kind == "create":
        metadata_overrides = {
            "final_slug": body.final_slug,
            "final_title": body.final_title,
            "final_page_type": body.final_page_type,
            "final_knowledge_type_slugs": body.final_knowledge_type_slugs,
        }

    try:
        page = await wiki_service.approve_draft(
            db, draft, user.id,
            reviewer_note=body.reviewer_note,
            edited_content_md=body.edited_content_md,
            allow_conflict=body.allow_conflict,
            metadata_overrides=metadata_overrides,
        )
    except wiki_service.DraftConflictError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "draft_conflict",
                "message": str(e),
                "current_version": e.current_version,
                "base_version": e.base_version,
                "hint": "Re-submit with allow_conflict=true to overwrite, or supply edited_content_md.",
            },
        )
    except wiki_service.CreateDraftSlugConflict as e:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "slug_conflict",
                "message": str(e),
                "slug": e.slug,
                "scope_type": e.scope_type,
                "scope_id": str(e.scope_id) if e.scope_id else None,
                "hint": "Override final_slug, or have the contributor edit the existing page instead.",
            },
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    action_label = "created" if draft.draft_kind == "create" else "approved"
    await log_audit(
        db, user, "update", "wiki_draft", str(draft.id),
        reason=f"{action_label} draft for: {page.slug}",
    )
    # Keep _index and _log fresh after content lands.
    scope_type = page.scope_type or "global"
    scope_id = page.scope_id
    await wiki_service.regenerate_index(db, scope_type=scope_type, scope_id=scope_id)
    await wiki_service.append_log(
        db,
        f"{action_label.capitalize()} page: {page.title} ({page.slug}) → v{page.version} by {user.name or user.email}",
        scope_type=scope_type,
        scope_id=scope_id,
    )
    draft.page = page
    await contribution_service.notify_approved(
        db, wiki_draft_adapter, draft, user, version_label=f"v{page.version}",
    )
    await db.commit()
    await db.refresh(draft)
    return await _draft_response(db, draft)


@router.post("/wiki/drafts/{draft_id}/reject", response_model=DraftResponse)
async def reject_draft(
    draft_id: str,
    body: RejectDraftRequest,
    db: AsyncSession = Depends(get_db),
    user: Employee = Depends(get_current_user),
):
    """Reject a pending draft. reviewer_note is required."""
    draft = await _load_draft(db, draft_id)
    if draft.status != "pending":
        raise HTTPException(400, f"Draft is already {draft.status}")

    if not await _can_review_draft(db, user, draft):
        raise HTTPException(403, "Insufficient permission to reject this draft")

    if draft.page_id:
        page = await db.get(WikiPage, draft.page_id)
        if page:
            draft.page = page
            slug_label = page.slug
        else:
            slug_label = "(unknown)"
    else:
        slug_label = (draft.suggested_metadata or {}).get("slug", "(new page)")

    await wiki_service.reject_draft(db, draft, user.id, body.reviewer_note)
    await log_audit(db, user, "update", "wiki_draft", str(draft.id), reason=f"rejected draft for: {slug_label}")
    await contribution_service.notify_rejected(
        db, wiki_draft_adapter, draft, user, reason=body.reviewer_note,
    )
    await db.commit()
    await db.refresh(draft)
    return await _draft_response(db, draft)


# ---------------------------------------------------------------------------
# needs_revision flow — request changes, resubmit, withdraw, rounds history
# ---------------------------------------------------------------------------

@router.post("/wiki/drafts/{draft_id}/request-changes", response_model=DraftResponse)
async def request_changes_on_draft(
    draft_id: str,
    body: RequestChangesRequest,
    db: AsyncSession = Depends(get_db),
    user: Employee = Depends(get_current_user),
):
    """Send a pending draft back to the author for revisions.

    Required `reviewer_note` explains what to fix. Author may then PATCH
    `/wiki/drafts/{id}/content` with new content to flip the draft back to
    pending. The draft is not deleted — its `revision_round` increments on
    each resubmission.
    """
    draft = await _load_draft(db, draft_id)
    if not await _can_review_draft(db, user, draft):
        raise HTTPException(403, "Insufficient permission to review this draft")

    if draft.page_id:
        page = await db.get(WikiPage, draft.page_id)
        if page:
            draft.page = page
    try:
        await contribution_service.request_changes(
            db, wiki_draft_adapter, draft, user, body.reviewer_note,
        )
    except InvalidTransition as e:
        raise HTTPException(400, str(e))
    await db.commit()
    await db.refresh(draft)
    return await _draft_response(db, draft)


@router.patch("/wiki/drafts/{draft_id}/content", response_model=DraftResponse)
async def resubmit_draft(
    draft_id: str,
    body: ResubmitDraftRequest,
    db: AsyncSession = Depends(get_db),
    user: Employee = Depends(get_current_user),
):
    """Author resubmits a draft after a request_changes round.

    Bumps `revision_round`, snapshots the prior submission to
    `wiki_draft_rounds`, clears `last_returned_note`, and flips the status
    back to pending so reviewers can look at it again.
    """
    draft = await _load_draft(db, draft_id)
    if draft.page_id:
        page = await db.get(WikiPage, draft.page_id)
        if page:
            draft.page = page

    try:
        await contribution_service.resubmit_wiki_draft(
            db, draft, user, body.content_md.strip(), author_note=body.note,
        )
    except InvalidTransition as e:
        raise HTTPException(400, str(e))
    await db.commit()
    await db.refresh(draft)
    return await _draft_response(db, draft)


@router.post("/wiki/drafts/{draft_id}/withdraw", response_model=DraftResponse)
async def withdraw_draft(
    draft_id: str,
    db: AsyncSession = Depends(get_db),
    user: Employee = Depends(get_current_user),
):
    """Author withdraws a pending or needs_revision draft. Admin override allowed."""
    draft = await _load_draft(db, draft_id)
    if draft.page_id:
        page = await db.get(WikiPage, draft.page_id)
        if page:
            draft.page = page

    try:
        await contribution_service.withdraw(db, wiki_draft_adapter, draft, user)
    except InvalidTransition as e:
        raise HTTPException(403, str(e))
    await db.commit()
    await db.refresh(draft)
    return await _draft_response(db, draft)


@router.get("/wiki/drafts/{draft_id}/rounds", response_model=list[DraftRoundResponse])
async def list_draft_rounds(
    draft_id: str,
    db: AsyncSession = Depends(get_db),
    user: Employee = Depends(get_current_user),
):
    """List prior submission rounds for a draft (review-trail audit).

    Visible to: author, reviewers of the page's scope, admin.
    """
    draft = await _load_draft(db, draft_id)
    if user.role != "admin" and draft.author_id != user.id:
        if not await _can_review_draft(db, user, draft):
            raise HTTPException(403, "Insufficient permission to view this draft's rounds")

    rows = (await db.execute(
        select(WikiDraftRound)
        .where(WikiDraftRound.draft_id == draft.id)
        .order_by(WikiDraftRound.round_no.asc())
    )).scalars().all()
    return [
        DraftRoundResponse(
            id=r.id,
            round_no=r.round_no,
            content_md=r.content_md,
            author_note=r.author_note,
            reviewer_return_note=r.reviewer_return_note,
            ai_check_results=r.ai_check_results,
            submitted_at=r.submitted_at.isoformat(),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Create-kind drafts — propose a brand new page
# ---------------------------------------------------------------------------

@router.post("/wiki/drafts/create", response_model=DraftResponse, status_code=201)
async def propose_create_page(
    body: ProposeCreateRequest,
    db: AsyncSession = Depends(get_db),
    user: Employee = Depends(get_current_user),
):
    """Propose a brand new wiki page.

    Contributor+ may file this. The page does NOT exist yet — it gets
    materialised when an editor approves. Reviewer can override the
    contributor's suggested slug / title / page_type / knowledge_type_slugs
    before approve.
    """
    # Contributor-level check, mirroring _can_propose for edit drafts.
    if user.role != "admin":
        perms = _get_user_permissions(user)
        if body.scope_type == "department" and body.scope_id:
            # own_dept perm only counts when proposing in the user's own dept.
            if "wiki:write:all" not in perms and not (
                "wiki:write:own_dept" in perms and body.scope_id in user.department_ids
            ):
                raise HTTPException(
                    403,
                    "Insufficient permission to propose pages in this department",
                )
        else:
            if not has_any_permission(list(perms), "wiki", "write"):
                raise HTTPException(403, "Insufficient permission to propose new pages")

    # Refuse if the slug already exists in the target scope (the contributor
    # should propose an edit on the existing page instead).
    existing = await wiki_service.get_page_by_slug(
        db, body.slug, scope_type=body.scope_type, scope_id=body.scope_id,
    )
    if existing is not None:
        raise HTTPException(
            409,
            f"Slug '{body.slug}' already exists in {body.scope_type}. "
            "Use propose_wiki_edit to edit the existing page.",
        )

    suggested_metadata = {
        "slug": body.slug,
        "title": body.title,
        "page_type": body.page_type,
        "knowledge_type_slugs": body.knowledge_type_slugs,
        "scope_type": body.scope_type,
        "scope_id": str(body.scope_id) if body.scope_id else None,
    }

    draft = await wiki_service.create_draft(
        db,
        page_id=None,
        author_id=user.id,
        content_md=body.content_md,
        note=body.note,
        source="web_ui",
        base_version=None,
        draft_kind="create",
        suggested_metadata=suggested_metadata,
    )
    if hasattr(body, "branch_id") and body.branch_id:
        draft.branch_id = body.branch_id
    await log_audit(
        db, user, "create", "wiki_draft", str(draft.id),
        reason=f"propose new page: {body.slug}",
    )
    await contribution_service.notify_submitted(db, wiki_draft_adapter, draft, user)
    await db.commit()
    await db.refresh(draft)
    return await _draft_response(db, draft)


# ---------------------------------------------------------------------------
# Bulk approve — for the reviewer queue page
# ---------------------------------------------------------------------------

@router.post("/wiki/drafts/bulk-approve", response_model=BulkApproveResponse)
async def bulk_approve_drafts(
    body: BulkApproveRequest,
    db: AsyncSession = Depends(get_db),
    user: Employee = Depends(get_current_user),
):
    """Approve many pending drafts in one request.

    Each draft is processed independently:
    - skipped when the draft is no longer pending or the user lacks permission
    - errored on conflict / domain validation failures (note returned)
    - approved otherwise

    The whole operation is committed at the end so partial successes persist
    even if a single draft errors.
    """
    results: list[BulkApproveItemResult] = []
    approved_count = 0
    skipped_count = 0
    errored_count = 0

    for did in body.draft_ids:
        draft = await db.get(WikiPageDraft, did)
        if not draft:
            results.append(BulkApproveItemResult(
                draft_id=did, status="error", message="Draft not found",
            ))
            errored_count += 1
            continue
        if draft.status != "pending":
            results.append(BulkApproveItemResult(
                draft_id=did, status="skipped",
                message=f"Draft is already {draft.status}",
            ))
            skipped_count += 1
            continue
        if not await _can_review_draft(db, user, draft):
            results.append(BulkApproveItemResult(
                draft_id=did, status="skipped",
                message="Insufficient permission",
            ))
            skipped_count += 1
            continue
        if user.role != "admin" and draft.author_id == user.id:
            results.append(BulkApproveItemResult(
                draft_id=did, status="skipped",
                message="Cannot approve your own draft",
            ))
            skipped_count += 1
            continue

        # Each draft gets its own SAVEPOINT so a mid-approve failure (conflict,
        # IntegrityError, etc.) rolls back just this draft instead of poisoning
        # the outer session for every subsequent iteration in the loop.
        #
        # IMPORTANT: SAVEPOINT rollback only reverts the DB — in-memory ORM
        # attribute mutations (e.g. page.version += 1) stay on the identity-map
        # instance. On error we MUST expire the touched objects so a later
        # iteration that approves another draft on the same page re-reads the
        # real version from DB, not the polluted value from the failed attempt.
        page = None
        try:
            async with db.begin_nested():
                page = await wiki_service.approve_draft(
                    db, draft, user.id,
                    reviewer_note=body.reviewer_note,
                    allow_conflict=body.allow_conflict,
                )
                await log_audit(
                    db, user, "update", "wiki_draft", str(draft.id),
                    reason=f"bulk-approved: {page.slug}",
                )
                scope_type = page.scope_type or "global"
                scope_id = page.scope_id
                await wiki_service.regenerate_index(db, scope_type=scope_type, scope_id=scope_id)
                await wiki_service.append_log(
                    db,
                    f"Bulk-approved draft for: {page.title} ({page.slug}) -> v{page.version} by {user.name or user.email}",
                    scope_type=scope_type, scope_id=scope_id,
                )
                draft.page = page
                await contribution_service.notify_approved(
                    db, wiki_draft_adapter, draft, user,
                    version_label=f"v{page.version}",
                )
        except (wiki_service.DraftConflictError, wiki_service.CreateDraftSlugConflict) as e:
            _expire_after_failed_approve(db, draft, page)
            results.append(BulkApproveItemResult(
                draft_id=did, status="error", message=str(e),
            ))
            errored_count += 1
            continue
        except Exception as e:
            _expire_after_failed_approve(db, draft, page)
            results.append(BulkApproveItemResult(
                draft_id=did, status="error", message=str(e),
            ))
            errored_count += 1
            continue

        results.append(BulkApproveItemResult(
            draft_id=did, status="approved",
            page_version=page.version,
        ))
        approved_count += 1

    await db.commit()
    return BulkApproveResponse(
        results=results,
        approved=approved_count,
        skipped=skipped_count,
        errored=errored_count,
    )
