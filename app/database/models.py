"""
SQLAlchemy ORM models for all database tables.

Permission Architecture v2 — Dual-Realm:
  - Global Realm: scoped permissions (doc:read:own_dept, doc:read:all, etc.)
  - Workspace Realm: membership-gated (viewer / contributor / editor / admin)
"""

import uuid
from datetime import datetime
from enum import Enum as PyEnum
from typing import Optional

from pgvector.sqlalchemy import HALFVEC, Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    case,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.dialects.postgresql import ENUM as PgEnum
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ScopeType(str, PyEnum):
    """Scope for sources/wiki: global or department."""
    GLOBAL = "global"
    DEPARTMENT = "department"


class SkillContributionStatus(str, PyEnum):
    """Status of a skill contribution request."""
    DRAFT = "draft"
    PENDING = "pending"
    NEEDS_REVISION = "needs_revision"
    WITHDRAWN = "withdrawn"
    APPROVED = "approved"
    REJECTED = "rejected"


# Status strings used by WikiPageDraft. Kept as a tuple (not Enum) because the
# column was historically `String(20)` with free-form values; centralising the
# set here lets services validate transitions consistently.
WIKI_DRAFT_STATUSES: tuple[str, ...] = (
    "pending",
    "needs_revision",
    "withdrawn",
    "approved",
    "rejected",
)


class Base(DeclarativeBase):
    """Base class for all models."""
    pass


# ---------------------------------------------------------------------------
# Sources — raw documents (file/URL)
# ---------------------------------------------------------------------------

class Source(Base):
    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    title: Mapped[Optional[str]] = mapped_column(String(500))
    full_text: Mapped[Optional[str]] = mapped_column(Text)
    source_type: Mapped[Optional[str]] = mapped_column(String(50))  # "file", "url"
    # --- Scope: global or project (workspace) ---
    scope_type: Mapped[str] = mapped_column(
        String(20), default=ScopeType.GLOBAL.value,
        comment="Scope type: global or project",
    )
    scope_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True,
        comment="Project/workspace ID when scope_type=project. Null for global.",
    )
    knowledge_type_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_types.id", ondelete="SET NULL"),
        nullable=True,
    )
    contributed_by_employee_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True,
    )
    file_path: Mapped[Optional[str]] = mapped_column(String(1000))
    url: Mapped[Optional[str]] = mapped_column(String(2000))
    minio_key: Mapped[Optional[str]] = mapped_column(String(500))
    file_name: Mapped[Optional[str]] = mapped_column(String(500))
    file_size: Mapped[Optional[int]] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(50), default="pending")
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    progress_message: Mapped[Optional[str]] = mapped_column(String(500))
    job_id: Mapped[Optional[str]] = mapped_column(String(200))
    extracted_token_count: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
        comment="tiktoken cl100k_base count of full_text. Used by upload gate.",
    )
    auto_recover_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
        comment="Times sweep_stuck_processing_cron has flipped this source from "
                "'processing' back to 'error'. Reset on successful plan_ready/ready. "
                "Gated by settings.max_auto_recover_attempts.",
    )
    pipeline_strategy: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True,
        comment="single_pass | standard | hierarchical — set by Phase 0 triage",
    )
    pipeline_phase: Mapped[Optional[str]] = mapped_column(
        String(30), nullable=True,
        comment="Current MRP phase: map | reduce | plan_review | refine | verify | commit",
    )
    # Heading-based TOC tree (PageIndex-style) built at ingest time from extracted markdown.
    # Schema: [{"title": str, "level": int, "page": int, "char_start": int, "char_end": int, "children": [...]}]
    outline_json: Mapped[Optional[list]] = mapped_column(JSONB)
    # Char offset (in full_text) where each extracted page begins.
    # Used by MCP `get_source_pages` to slice raw text by page range.
    page_offsets: Mapped[Optional[list[int]]] = mapped_column(JSONB)
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    departments: Mapped[list["SourceDepartment"]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )
    knowledge_type: Mapped[Optional["KnowledgeType"]] = relationship()
    contributor: Mapped[Optional["Employee"]] = relationship(
        foreign_keys=[contributed_by_employee_id]
    )


class SourceDepartment(Base):
    """Many-to-many: Source ↔ Department.
    A source with NO rows here is considered Global (visible to all).
    """
    __tablename__ = "source_departments"

    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"),
        primary_key=True,
    )
    department_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("departments.id", ondelete="CASCADE"),
        primary_key=True,
    )

    # Relationships
    source: Mapped["Source"] = relationship(back_populates="departments")
    department: Mapped["Department"] = relationship(back_populates="source_departments")


class SourceImage(Base):
    """An image extracted from a source document during ingestion.

    Wiki pages reference these by id via `image://<uuid>` markers in content_md.
    The wiki compiler decides which page each image belongs to based on context.
    """
    __tablename__ = "source_images"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    minio_key: Mapped[str] = mapped_column(Text, nullable=False)
    page_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    image_index: Mapped[int] = mapped_column(Integer, nullable=False)
    caption: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("source_id", "image_index", name="uq_source_images_source_idx"),
    )

    source: Mapped["Source"] = relationship()


# ---------------------------------------------------------------------------
# MRP Pipeline — MAP/REDUCE/PLAN/REFINE/VERIFY compilation state
# ---------------------------------------------------------------------------

class SourceChunkExtract(Base):
    """Phase 1 MAP output: structured knowledge extracted from one document chunk.

    Each row corresponds to a ~20k-char section of the source document.
    Stored immediately after extraction so the pipeline can resume if interrupted.
    """
    __tablename__ = "source_chunk_extracts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    start_char: Mapped[int] = mapped_column(Integer, nullable=False)
    end_char: Mapped[int] = mapped_column(Integer, nullable=False)
    section_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    extract_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("source_id", "chunk_index", name="uq_sce_source_chunk"),
        Index("ix_sce_source_status", "source_id", "status"),
    )

    source: Mapped["Source"] = relationship()


class SourceCompilationPlan(Base):
    """Phase 2 REDUCE output: compilation plan listing pages to create/update.

    One plan per source. Status flow:
    pending_review → approved (→ in_progress → done) | rejected
    """
    __tablename__ = "source_compilation_plans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=False, unique=True,
    )
    plan_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending_review")
    reviewed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("employees.id", ondelete="SET NULL"), nullable=True,
    )
    review_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_scp_status", "status"),
    )

    source: Mapped["Source"] = relationship()
    reviewer: Mapped[Optional["Employee"]] = relationship(foreign_keys=[reviewed_by])


# ---------------------------------------------------------------------------
# Wiki — LLM-compiled persistent knowledge layer
# ---------------------------------------------------------------------------

class WikiPage(Base):
    """
    A markdown wiki page maintained by the LLM Wiki Compiler.
    Reserved slugs: '_index' (catalog), '_log' (chronological activity log).
    """
    __tablename__ = "wiki_pages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    slug: Mapped[str] = mapped_column(String(300), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="seed",
        comment="Lifecycle status: seed | developing | mature | evergreen"
    )
    content_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # --- Scope: global or project (workspace) ---
    scope_type: Mapped[str] = mapped_column(
        String(20), default=ScopeType.GLOBAL.value,
        comment="Scope type: global or project",
    )
    scope_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True,
        comment="Project/workspace ID. Null for global scope.",
    )
    knowledge_type_slugs: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list,
    )
    source_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, default=list,
    )
    # Embeddings live in per-dimension tables (wiki_page_embeddings_<dim>) so
    # different embedding models with different output sizes can coexist.
    # See app/ai/embedding_catalog.py and migration 015.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    orphaned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @hybrid_property
    def page_type(self) -> str:
        if self.slug in ("_index", "_log", "_hot"):
            return self.slug.lstrip("_")
        if self.slug.startswith("source/"):
            return "source"
        return "concept"

    @page_type.expression
    @classmethod
    def page_type(cls):
        return case(
            (cls.slug == "_index", "index"),
            (cls.slug == "_log", "log"),
            (cls.slug == "_hot", "hot"),
            (cls.slug.like("source/%"), "source"),
            else_="concept"
        )

    __table_args__ = (
        Index("ix_wiki_pages_status", "status"),
    )


class WikiLink(Base):
    """
    Derived edge from a wiki page to a target slug, parsed from `[[slug]]`
    patterns in content_md. Origin is keyed by page_id so edges are scope-
    disambiguated when the same slug exists in multiple scopes. Target stays
    a slug because dangling links to not-yet-existing pages are valid.
    Refreshed after every page upsert by wiki_service.refresh_links().
    """
    __tablename__ = "wiki_links"

    from_page_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("wiki_pages.id", ondelete="CASCADE"),
        nullable=False,
    )
    to_slug: Mapped[str] = mapped_column(String(300), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("from_page_id", "to_slug"),
        Index("ix_wiki_links_from_page_id", "from_page_id"),
        Index("ix_wiki_links_to_slug", "to_slug"),
    )


class WikiBranch(Base):
    """
    Named contribution branch grouping multiple page drafts.
    """
    __tablename__ = "wiki_branches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    scope_type: Mapped[str] = mapped_column(String(20), nullable=False, default="global")
    scope_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    author_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    has_conflict: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reviewer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewer_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    author: Mapped["Employee"] = relationship("Employee", foreign_keys=[author_id])
    reviewer: Mapped[Optional["Employee"]] = relationship("Employee", foreign_keys=[reviewer_id])
    drafts: Mapped[list["WikiPageDraft"]] = relationship(
        "WikiPageDraft", back_populates="branch", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_wiki_branches_author_id", "author_id"),
        Index("ix_wiki_branches_status", "status"),
    )


class WikiPageDraft(Base):
    """
    Pending contribution proposed by a workspace member.
    An editor/admin reviews and either approves (writing to wiki_pages.content_md)
    or rejects (with a reviewer_note explaining why).
    Multiple drafts per page are allowed — editor resolves all.
    """
    __tablename__ = "wiki_page_drafts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # NULL only when draft_kind='create' — the page is materialised at approval.
    page_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wiki_pages.id", ondelete="CASCADE"), nullable=True
    )
    # 'edit' (default): modifies the page referenced by page_id.
    # 'create': proposes a brand new page; suggested_metadata holds slug,
    # title, page_type, knowledge_type_slugs, scope_type, scope_id.
    draft_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="edit")
    suggested_metadata: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    author_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    content_md: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # version of the target page when this draft was authored; compared at
    # approve-time to detect mid-air collisions (None = pre-migration drafts).
    base_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Increments each time the author resubmits after needs_revision.
    revision_round: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Reviewer's note when sending the draft back for revisions.
    last_returned_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # pending | running | passed | warned | failed — set by AI pre-review worker.
    ai_check_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # See app/services/ai_review/runner.py for the JSON shape.
    ai_check_results: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    ai_checked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # pending | needs_revision | withdrawn | approved | rejected
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # web_ui | mcp_claude_desktop | mcp_claude_code | mcp_other | api_direct
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="web_ui")
    source_metadata: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    branch_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wiki_branches.id", ondelete="CASCADE"), nullable=True
    )
    reviewed_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewer_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    page: Mapped["WikiPage"] = relationship("WikiPage", foreign_keys=[page_id])
    author: Mapped[Optional["Employee"]] = relationship("Employee", foreign_keys=[author_id])
    reviewer: Mapped[Optional["Employee"]] = relationship("Employee", foreign_keys=[reviewed_by_id])
    branch: Mapped[Optional["WikiBranch"]] = relationship("WikiBranch", back_populates="drafts")

    __table_args__ = (
        Index("ix_wiki_drafts_page_id", "page_id"),
        Index("ix_wiki_drafts_status", "status"),
        Index("ix_wiki_drafts_author_id", "author_id"),
        Index("ix_wiki_drafts_branch_id", "branch_id"),
    )


class WikiPageRevision(Base):
    """
    Immutable snapshot of wiki page content at each version.
    Created on every content-changing operation: agent compile, editor edit,
    draft approval, manual rebuild, rollback.
    """
    __tablename__ = "wiki_page_revisions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    page_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wiki_pages.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_md: Mapped[str] = mapped_column(Text, nullable=False)
    # agent_compile | agent_retry | editor_edit | draft_approved | manual_rebuild | rollback
    change_type: Mapped[str] = mapped_column(String(30), nullable=False)
    draft_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wiki_page_drafts.id", ondelete="SET NULL"), nullable=True
    )
    changed_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    change_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_wiki_revisions_page_id", "page_id"),
        # Unique constraint, not just an index — guards against the
        # historical race where two concurrent approves could both INSERT a
        # revision row at the same version. The advisory lock in
        # wiki_service.approve_draft prevents this in normal operation; this
        # constraint is the DB-level backstop.
        Index("uq_wiki_revisions_page_version", "page_id", "version", unique=True),
    )


class WikiDraftRound(Base):
    """
    Snapshot of a draft's content for one review round. A new row is appended
    every time a reviewer sends the draft back for revisions — capturing the
    content the author had submitted and the note that bounced it back. The
    next author resubmission updates the parent draft and creates the next
    round on the *following* request_changes call.
    """
    __tablename__ = "wiki_draft_rounds"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    draft_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("wiki_page_drafts.id", ondelete="CASCADE"),
        nullable=False,
    )
    round_no: Mapped[int] = mapped_column(Integer, nullable=False)
    content_md: Mapped[str] = mapped_column(Text, nullable=False)
    author_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reviewer_return_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # AI verdict at the time this round was sent back — frozen so reviewers
    # can compare AI checks across rounds.
    ai_check_results: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_wiki_draft_rounds_draft_id", "draft_id", "round_no"),
    )


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

class Note(Base):
    __tablename__ = "notes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    title: Mapped[Optional[str]] = mapped_column(String(500))
    content: Mapped[Optional[str]] = mapped_column(Text)
    note_type: Mapped[Optional[str]] = mapped_column(String(50))  # "human", "ai"
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ---------------------------------------------------------------------------
# App Config (key-value store for settings)
# ---------------------------------------------------------------------------

class AppConfig(Base):
    __tablename__ = "app_config"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ---------------------------------------------------------------------------
# Knowledge Types (admin-defined, dynamic)
# ---------------------------------------------------------------------------

class KnowledgeType(Base):
    """
    Admin-defined knowledge type — replaces hardcoded types.
    Examples: SOP, Product, HR Policy, Technical Spec, etc.
    """
    __tablename__ = "knowledge_types"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    slug: Mapped[str] = mapped_column(
        String(50), nullable=False, unique=True,
        comment="URL-safe identifier, e.g. 'sop', 'product', 'hr-policy'",
    )
    name: Mapped[str] = mapped_column(
        String(100), nullable=False,
        comment="Display name, e.g. 'Standard Operating Procedure'",
    )
    color: Mapped[Optional[str]] = mapped_column(
        String(20), default="#6366f1",
        comment="Hex color for UI badge",
    )
    description: Mapped[Optional[str]] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ---------------------------------------------------------------------------
# RBAC: Roles, Departments, Employees
# ---------------------------------------------------------------------------



class Department(Base):
    """Organizational department — groups employees and scopes knowledge access."""
    __tablename__ = "departments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    description: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    employee_departments: Mapped[list["EmployeeDepartment"]] = relationship(
        back_populates="department",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    employees: Mapped[list["Employee"]] = relationship(
        secondary="employee_departments",
        back_populates="departments",
        viewonly=True,
    )
    source_departments: Mapped[list["SourceDepartment"]] = relationship(
        back_populates="department", cascade="all, delete-orphan"
    )
    skill_departments: Mapped[list["SkillDepartment"]] = relationship(
        back_populates="department", cascade="all, delete-orphan"
    )


class Employee(Base):
    """
    Employee — authenticates via login (JWT) or MCP token.
    Role 'admin' has full access (bypasses all permission checks).
    Role 'employee' access is governed by custom_role permissions.
    """
    __tablename__ = "employees"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    password_hash: Mapped[Optional[str]] = mapped_column(
        String(500),
        comment="bcrypt hash of password",
    )
    role: Mapped[str] = mapped_column(
        String(20), default="employee",
        comment="admin or employee — system-level role",
    )
    global_role: Mapped[str] = mapped_column(
        String(30), default="viewer",
        comment="viewer, contributor, knowledge_manager, or admin",
    )
    # Legacy plaintext column — kept nullable for one release so a rollback is
    # possible. The hashed column below is authoritative; new code never reads
    # or writes mcp_token. Drop in a follow-up migration.
    mcp_token: Mapped[Optional[str]] = mapped_column(
        String(500), unique=True,
        comment="DEPRECATED — legacy plaintext token, no longer read or written",
    )
    mcp_token_hash: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True,
        comment="HMAC-SHA256(pepper, token) — primary lookup key for MCP auth",
    )
    mcp_token_prefix: Mapped[Optional[str]] = mapped_column(
        String(12), nullable=True,
        comment="First 12 chars of the token for UI display (e.g. ark_aBcD…)",
    )
    mcp_token_rotated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_connected: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    employee_departments: Mapped[list["EmployeeDepartment"]] = relationship(
        back_populates="employee",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    departments: Mapped[list["Department"]] = relationship(
        secondary="employee_departments",
        back_populates="employees",
        viewonly=True,
    )


    __table_args__ = (
        Index("ix_employees_mcp_token", "mcp_token"),
        Index(
            "ix_employees_mcp_token_hash",
            "mcp_token_hash",
            unique=True,
            postgresql_where=text("mcp_token_hash IS NOT NULL"),
        ),
        Index("ix_employees_email", "email"),
    )

    @property
    def department_ids(self) -> list[uuid.UUID]:
        """All departments this employee belongs to. Empty list = no dept."""
        return [ed.department_id for ed in self.employee_departments]


class EmployeeDepartment(Base):
    """Many-to-many: Employee ↔ Department.

    All departments are equal — there is no concept of a "primary" department.
    `*:*:own_dept` permissions resolve to the union of all departments listed
    here for the employee. An employee with zero rows can only see resources
    scoped to 'global'.
    """
    __tablename__ = "employee_departments"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"),
        primary_key=True,
    )
    department_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("departments.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    employee: Mapped["Employee"] = relationship(back_populates="employee_departments")
    department: Mapped["Department"] = relationship(back_populates="employee_departments")

    __table_args__ = (
        Index("ix_employee_departments_department_id", "department_id"),
    )





# ---------------------------------------------------------------------------
# AI Skills — Versioned prompt packages and tools
# ---------------------------------------------------------------------------

class Skill(Base):
    """
    An AI Skill package (e.g. 'document-generator').
    Can be scoped to a department or global (NULL department).
    """
    __tablename__ = "skills"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    scope_type: Mapped[str] = mapped_column(
        String(20), default="global",
        comment="Scope type: global, project, department, team",
    )
    scope_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True,
        comment="Scope entity ID. Null for global scope.",
    )
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    version_hash: Mapped[Optional[str]] = mapped_column(String(64))
    storage_path: Mapped[Optional[str]] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(
        PgEnum("active", "processing", "deleting", "deprecated", "archived", name="skill_status"),
        server_default="active",
        nullable=False,
    )
    is_system: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false",
        comment="True for skills seeded from source code. Immutable via API.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    departments: Mapped[list["SkillDepartment"]] = relationship(
        "SkillDepartment",
        back_populates="skill",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    versions: Mapped[list["SkillVersion"]] = relationship(
        back_populates="skill", cascade="all, delete-orphan"
    )
    contributions: Mapped[list["SkillContribution"]] = relationship(
        back_populates="skill", cascade="all, delete-orphan"
    )


class SkillDepartment(Base):
    """Many-to-many: Skill ↔ Department.
    A skill with NO rows here is considered Global (visible to all).
    """
    __tablename__ = "skill_departments"

    skill_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("skills.id", ondelete="CASCADE"),
        primary_key=True,
    )
    department_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("departments.id", ondelete="CASCADE"),
        primary_key=True,
    )

    # Relationships
    skill: Mapped["Skill"] = relationship(back_populates="departments")
    department: Mapped["Department"] = relationship(back_populates="skill_departments")


class SkillVersion(Base):
    """Specific version of a skill."""
    __tablename__ = "skill_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    skill_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("skills.id", ondelete="CASCADE")
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    version_hash: Mapped[Optional[str]] = mapped_column(String(64))
    storage_path: Mapped[Optional[str]] = mapped_column(String(1000))
    changelog: Mapped[Optional[str]] = mapped_column(Text)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    skill: Mapped["Skill"] = relationship(back_populates="versions")
    author: Mapped[Optional["Employee"]] = relationship()

    __table_args__ = (
        Index("ix_skill_versions_skill_id", "skill_id"),
    )



# ---------------------------------------------------------------------------
# Scope-based RBAC: Membership & Audit
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

class AuditLog(Base):
    """
    Append-only access decision log.
    Records actions for compliance and debugging.
    """
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False,
        comment="Employee or agent ID",
    )
    principal_type: Mapped[str] = mapped_column(
        String(20), default="human",
        comment="human or agent",
    )
    action: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="Action attempted (read, list, delete...)",
    )
    resource_type: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="Type of resource: source, wiki_page, etc.",
    )
    resource_id: Mapped[str] = mapped_column(
        String(100), nullable=False,
        comment="UUID or identifier of the resource",
    )
    decision: Mapped[str] = mapped_column(
        String(10), nullable=False,
        comment="allow or deny",
    )
    reason: Mapped[Optional[str]] = mapped_column(
        Text,
        comment="Human-readable reason for the decision",
    )
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONB,
        comment="Extra context (IP, user agent, request ID...)",
    )

    __table_args__ = (
        Index("ix_audit_log_timestamp", "timestamp"),
        Index("ix_audit_log_principal", "principal_id"),
        Index("ix_audit_log_resource", "resource_type", "resource_id"),
    )


class Notification(Base):
    """
    In-app notification delivered to one recipient. Created synchronously by
    NotificationService when a contribution lifecycle event fires. Read state
    is tracked per-row (read_at timestamp). No retention policy yet — caller
    can prune by created_at if the table grows.
    """
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    recipient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
    )
    # e.g. "wiki_draft.submitted", "skill_contribution.approved"
    type: Mapped[str] = mapped_column(String(80), nullable=False)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # target_type/target_id: a generic pointer (wiki_draft + UUID, etc.) so the
    # frontend can deep-link without us joining at query time.
    target_type: Mapped[str] = mapped_column(String(40), nullable=False)
    target_id: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True,
        comment="Employee who caused the event (author/reviewer)",
    )
    read_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_notifications_recipient_unread", "recipient_id", "read_at"),
        Index("ix_notifications_created_at", "created_at"),
        Index("ix_notifications_target", "target_type", "target_id"),
    )


# ---------------------------------------------------------------------------
# Multi-dimension wiki page embeddings
# ---------------------------------------------------------------------------
# One table per supported output dimension. The active embedding model spec
# (stored in app_config.active_embedding_model_spec_id) determines which table
# search & ingestion read/write. See app/ai/embedding_catalog.py.

class _WikiPageEmbeddingBase:
    """Mixin: shared columns for all wiki_page_embeddings_<dim> tables."""

    page_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("wiki_pages.id", ondelete="CASCADE"),
        primary_key=True,
    )
    model_spec_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WikiPageEmbedding768(_WikiPageEmbeddingBase, Base):
    __tablename__ = "wiki_page_embeddings_768"
    embedding = mapped_column(Vector(768), nullable=False)


class WikiPageEmbedding1024(_WikiPageEmbeddingBase, Base):
    __tablename__ = "wiki_page_embeddings_1024"
    embedding = mapped_column(Vector(1024), nullable=False)


class WikiPageEmbedding1536(_WikiPageEmbeddingBase, Base):
    __tablename__ = "wiki_page_embeddings_1536"
    embedding = mapped_column(Vector(1536), nullable=False)


class WikiPageEmbedding3072(_WikiPageEmbeddingBase, Base):
    # 3072d uses halfvec — pgvector's HNSW index caps `vector` at 2000 dims.
    __tablename__ = "wiki_page_embeddings_3072"
    embedding = mapped_column(HALFVEC(3072), nullable=False)


_EMBEDDING_MODEL_BY_DIM: dict[int, type] = {
    768: WikiPageEmbedding768,
    1024: WikiPageEmbedding1024,
    1536: WikiPageEmbedding1536,
    3072: WikiPageEmbedding3072,
}


def get_embedding_model_for_dim(dimension: int) -> type:
    """Return the WikiPageEmbedding<dim> ORM class for a supported dimension."""
    try:
        return _EMBEDDING_MODEL_BY_DIM[dimension]
    except KeyError as e:
        raise ValueError(
            f"Unsupported embedding dimension: {dimension}. "
            f"Supported: {sorted(_EMBEDDING_MODEL_BY_DIM)}"
        ) from e


class EmbeddingJob(Base):
    """Tracks a background re-embed job triggered when admin switches model."""

    __tablename__ = "embedding_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    model_spec_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending"
    )  # pending | running | completed | failed | cancelled
    total_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    done_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_embedding_jobs_status", "status", "created_at"),
    )


# Skill Contributions — Pull Request style workflow
# ---------------------------------------------------------------------------

class SkillContribution(Base):
    """
    A request to create a new skill or update an existing one.
    Acts as a 'Pull Request' where files are stored in a temporary path
    until approved by an admin.
    """
    __tablename__ = "skill_contributions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    skill_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("skills.id", ondelete="CASCADE"),
        nullable=True,
    )
    contributor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE")
    )
    base_version: Mapped[Optional[int]] = mapped_column(
        Integer,
        comment="Version number this contribution was forked from. Null for new skills.",
    )
    status: Mapped[str] = mapped_column(
        String(20), default=SkillContributionStatus.DRAFT.value,
        index=True
    )
    # Increments each time the contributor resubmits after needs_revision.
    revision_round: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Reviewer's note when sending the contribution back for changes.
    last_returned_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    scope_type: Mapped[str] = mapped_column(
        String(20), default="global",
        comment="Scope type for NEW skills: global or department",
    )
    scope_ids: Mapped[Optional[list]] = mapped_column(
        JSONB, nullable=True,
        comment="List of Department IDs if scope_type is department",
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    storage_path: Mapped[Optional[str]] = mapped_column(
        String(1000),
        comment="MinIO prefix for this contribution's files, e.g. 'skill-contributions/{id}/'",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    skill: Mapped[Optional["Skill"]] = relationship(back_populates="contributions")
    contributor: Mapped["Employee"] = relationship()

    __table_args__ = (
        Index("ix_skill_contributions_contributor_id", "contributor_id"),
        Index("ix_skill_contributions_status", "status"),
    )


# ---------------------------------------------------------------------------
# MCP query log — one row per MCP tool call (for usage analytics & gap detection)
# ---------------------------------------------------------------------------

class MCPQueryLog(Base):
    __tablename__ = "mcp_query_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    employee_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True,
        comment="Caller; NULL if token resolution failed before call",
    )
    tool_name: Mapped[str] = mapped_column(
        String(80), nullable=False,
        comment="MCP tool invoked: search_wiki, read_wiki_page, propose_wiki_edit, ...",
    )
    query_text: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="Search/query string when applicable",
    )
    result_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    scope_metadata: Mapped[Optional[dict]] = mapped_column(
        JSONB, nullable=True,
        comment="Department/project/filters used for the call",
    )
    result_ids: Mapped[Optional[list]] = mapped_column(
        JSONB, nullable=True,
        comment="IDs returned (wiki_page_id or source_id list)",
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="ok",
        comment="ok | error | denied",
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_mcp_query_log_created_at", "created_at"),
        Index("ix_mcp_query_log_employee_id", "employee_id"),
        Index("ix_mcp_query_log_tool_name", "tool_name"),
        Index("ix_mcp_query_log_zero_result", "created_at", "result_count"),
    )


# ---------------------------------------------------------------------------
# Stats daily rollup — pre-aggregated metrics for the admin dashboard
# ---------------------------------------------------------------------------

class StatsDailyRollup(Base):
    """
    One row per (date, metric_key, dimensions). value_numeric for scalar metrics;
    value_json for top-N lists or structured payloads (top contributors, gap topics).
    """
    __tablename__ = "stats_daily_rollup"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        comment="UTC date the metric covers (midnight UTC)",
    )
    metric_key: Mapped[str] = mapped_column(
        String(80), nullable=False,
        comment="e.g. wiki.pages.total, mcp.queries.zero_result, draft.pending",
    )
    dimensions: Mapped[Optional[dict]] = mapped_column(
        JSONB, nullable=True,
        comment="{department_id, project_id, tool_name, source}",
    )
    dimensions_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, default="",
        comment="md5 of canonical-serialized dimensions; empty string when dimensions is NULL",
    )
    value_numeric: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    value_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("date", "metric_key", "dimensions_hash", name="uq_stats_rollup_keys"),
        Index("ix_stats_rollup_date", "date"),
        Index("ix_stats_rollup_metric", "metric_key", "date"),
    )

