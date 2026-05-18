"""
Daily rollup aggregator for the admin Statistics dashboard.

`run_daily_rollup(target_date)` recomputes all rollup metrics for the UTC day
that starts at `target_date 00:00`. Idempotent — re-running for the same date
overwrites the existing rows via UPSERT on (date, metric_key, dimensions_hash).

Metric categories (see also docs in `app/routers/admin_stats.py`):
    Content       — wiki.pages.*, wiki.revisions.*, wiki.top_pages
    Contribution  — draft.*, compile_plan.pending_review
    Usage         — mcp.queries.*, mcp.active_users, mcp.top_employees
    Gaps          — mcp.gaps.zero_result
    Audit         — audit.denied, audit.denied.by_resource
"""

from __future__ import annotations

import hashlib
import json
import re
import string
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from loguru import logger
from sqlalchemy import and_, distinct, func, not_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.database.models import (
    AuditLog,
    Employee,
    MCPQueryLog,
    SourceCompilationPlan,
    StatsDailyRollup,
    WikiLink,
    WikiPage,
    WikiPageDraft,
    WikiPageRevision,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STOPWORDS_EN = {
    "the", "a", "an", "of", "for", "to", "in", "on", "at", "and", "or",
    "is", "are", "was", "were", "be", "been", "what", "how", "why",
    "where", "when", "who", "do", "does", "did", "i", "we", "you", "it",
    "this", "that", "these", "those", "about", "with",
}
_STOPWORDS_VI = {
    "la", "lam", "co", "khong", "nao", "the", "nay", "do", "thi", "va",
    "hoac", "cua", "trong", "voi", "cho", "den", "ai", "gi", "sao",
    "minh", "ban", "ta", "ho", "duoc", "phai", "se", "da", "dang",
}


def _norm_text(text: str) -> str:
    """Lowercase, strip punctuation, drop stopwords. For grouping zero-result queries."""
    if not text:
        return ""
    cleaned = text.lower().translate(str.maketrans("", "", string.punctuation))
    tokens = [t for t in cleaned.split() if t and t not in _STOPWORDS_EN and t not in _STOPWORDS_VI]
    return " ".join(tokens)[:200]


def _dim_hash(dimensions: Optional[dict]) -> str:
    if not dimensions:
        return ""
    canonical = json.dumps(dimensions, sort_keys=True, default=str)
    return hashlib.md5(canonical.encode("utf-8")).hexdigest()


def _window(target_date: date) -> tuple[datetime, datetime]:
    """UTC half-open window [start, end) for the given date."""
    start = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start, end


# ---------------------------------------------------------------------------
# Upsert primitive
# ---------------------------------------------------------------------------

async def _upsert_metrics(
    session: AsyncSession,
    *,
    target_date: date,
    rows: Iterable[dict[str, Any]],
) -> int:
    """Bulk upsert into stats_daily_rollup. Each row dict: metric_key, dimensions?, value_numeric?, value_json?."""
    date_dt = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
    payload: list[dict[str, Any]] = []
    for r in rows:
        dims = r.get("dimensions")
        payload.append({
            "date": date_dt,
            "metric_key": r["metric_key"],
            "dimensions": dims,
            "dimensions_hash": _dim_hash(dims),
            "value_numeric": r.get("value_numeric"),
            "value_json": r.get("value_json"),
        })
    if not payload:
        return 0
    stmt = pg_insert(StatsDailyRollup).values(payload)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_stats_rollup_keys",
        set_={
            "value_numeric": stmt.excluded.value_numeric,
            "value_json": stmt.excluded.value_json,
            "computed_at": func.now(),
        },
    )
    await session.execute(stmt)
    return len(payload)


# ---------------------------------------------------------------------------
# Content health
# ---------------------------------------------------------------------------

async def _rollup_content(session: AsyncSession, target_date: date) -> list[dict[str, Any]]:
    win_start, win_end = _window(target_date)
    rows: list[dict[str, Any]] = []

    # Snapshot: total pages
    total_pages = (await session.execute(select(func.count(WikiPage.id)))).scalar() or 0
    rows.append({"metric_key": "wiki.pages.total", "value_numeric": float(total_pages)})

    # Pages created in the window
    created_today = (await session.execute(
        select(func.count(WikiPage.id)).where(
            and_(WikiPage.created_at >= win_start, WikiPage.created_at < win_end)
        )
    )).scalar() or 0
    rows.append({"metric_key": "wiki.pages.created", "value_numeric": float(created_today)})

    # Pages updated in the window
    updated_today = (await session.execute(
        select(func.count(WikiPage.id)).where(
            and_(WikiPage.updated_at >= win_start, WikiPage.updated_at < win_end)
        )
    )).scalar() or 0
    rows.append({"metric_key": "wiki.pages.updated", "value_numeric": float(updated_today)})

    # Stale: not updated in 30 days (snapshot)
    stale_cutoff = win_end - timedelta(days=30)
    stale = (await session.execute(
        select(func.count(WikiPage.id)).where(WikiPage.updated_at < stale_cutoff)
    )).scalar() or 0
    rows.append({"metric_key": "wiki.pages.stale_30d", "value_numeric": float(stale)})

    # Orphan: pages whose slug has no inbound WikiLink (or model flag)
    inbound_subq = select(WikiLink.to_slug).distinct().subquery()
    orphan = (await session.execute(
        select(func.count(WikiPage.id)).where(
            WikiPage.slug.notin_(select(inbound_subq.c.to_slug))
        )
    )).scalar() or 0
    rows.append({"metric_key": "wiki.pages.orphan", "value_numeric": float(orphan)})

    # Revisions in window (activity)
    revisions = (await session.execute(
        select(func.count(WikiPageRevision.id)).where(
            and_(WikiPageRevision.created_at >= win_start, WikiPageRevision.created_at < win_end)
        )
    )).scalar() or 0
    rows.append({"metric_key": "wiki.revisions.daily", "value_numeric": float(revisions)})

    # Pages by page_type (snapshot, dimensioned)
    type_rows = (await session.execute(
        select(WikiPage.page_type, func.count(WikiPage.id)).group_by(WikiPage.page_type)
    )).all()
    for page_type, n in type_rows:
        rows.append({
            "metric_key": "wiki.pages.by_type",
            "dimensions": {"page_type": page_type or "unknown"},
            "value_numeric": float(n),
        })

    # Top pages by revisions in window
    hot = (await session.execute(
        select(
            WikiPage.slug,
            WikiPage.title,
            func.count(WikiPageRevision.id).label("rev_count"),
        )
        .join(WikiPageRevision, WikiPageRevision.page_id == WikiPage.id)
        .where(and_(WikiPageRevision.created_at >= win_start, WikiPageRevision.created_at < win_end))
        .group_by(WikiPage.slug, WikiPage.title)
        .order_by(func.count(WikiPageRevision.id).desc())
        .limit(10)
    )).all()
    rows.append({
        "metric_key": "wiki.top_pages",
        "value_json": {"items": [
            {"slug": s, "title": t, "revisions": int(c)} for s, t, c in hot
        ]},
    })

    return rows


# ---------------------------------------------------------------------------
# Contribution & review
# ---------------------------------------------------------------------------

async def _rollup_contribution(session: AsyncSession, target_date: date) -> list[dict[str, Any]]:
    win_start, win_end = _window(target_date)
    rows: list[dict[str, Any]] = []

    created = (await session.execute(
        select(func.count(WikiPageDraft.id)).where(
            and_(WikiPageDraft.created_at >= win_start, WikiPageDraft.created_at < win_end)
        )
    )).scalar() or 0
    rows.append({"metric_key": "draft.created", "value_numeric": float(created)})

    approved = (await session.execute(
        select(func.count(WikiPageDraft.id)).where(and_(
            WikiPageDraft.reviewed_at >= win_start,
            WikiPageDraft.reviewed_at < win_end,
            WikiPageDraft.status == "approved",
        ))
    )).scalar() or 0
    rows.append({"metric_key": "draft.approved", "value_numeric": float(approved)})

    rejected = (await session.execute(
        select(func.count(WikiPageDraft.id)).where(and_(
            WikiPageDraft.reviewed_at >= win_start,
            WikiPageDraft.reviewed_at < win_end,
            WikiPageDraft.status == "rejected",
        ))
    )).scalar() or 0
    rows.append({"metric_key": "draft.rejected", "value_numeric": float(rejected)})

    pending = (await session.execute(
        select(func.count(WikiPageDraft.id)).where(WikiPageDraft.status == "pending")
    )).scalar() or 0
    rows.append({"metric_key": "draft.pending", "value_numeric": float(pending)})

    # Avg time-to-review (seconds) for drafts reviewed in window
    ttr = (await session.execute(
        select(func.avg(
            func.extract("epoch", WikiPageDraft.reviewed_at - WikiPageDraft.created_at)
        )).where(and_(
            WikiPageDraft.reviewed_at >= win_start,
            WikiPageDraft.reviewed_at < win_end,
        ))
    )).scalar()
    rows.append({"metric_key": "draft.time_to_review_avg_seconds", "value_numeric": float(ttr) if ttr is not None else None})

    # Drafts by source (web_ui vs mcp_*)
    by_source = (await session.execute(
        select(WikiPageDraft.source, func.count(WikiPageDraft.id))
        .where(and_(WikiPageDraft.created_at >= win_start, WikiPageDraft.created_at < win_end))
        .group_by(WikiPageDraft.source)
    )).all()
    for src, n in by_source:
        rows.append({
            "metric_key": "draft.created.by_source",
            "dimensions": {"source": src or "unknown"},
            "value_numeric": float(n),
        })

    # Top contributors (drafts created in window)
    top_authors = (await session.execute(
        select(
            WikiPageDraft.author_id,
            Employee.name,
            func.count(WikiPageDraft.id).label("n"),
        )
        .join(Employee, Employee.id == WikiPageDraft.author_id, isouter=True)
        .where(and_(WikiPageDraft.created_at >= win_start, WikiPageDraft.created_at < win_end))
        .group_by(WikiPageDraft.author_id, Employee.name)
        .order_by(func.count(WikiPageDraft.id).desc())
        .limit(10)
    )).all()
    rows.append({
        "metric_key": "draft.top_contributors",
        "value_json": {"items": [
            {"author_id": str(aid) if aid else None, "name": name or "(unknown)", "count": int(n)}
            for aid, name, n in top_authors
        ]},
    })

    # Top reviewers
    top_reviewers = (await session.execute(
        select(
            WikiPageDraft.reviewed_by_id,
            Employee.name,
            func.count(WikiPageDraft.id).label("n"),
        )
        .join(Employee, Employee.id == WikiPageDraft.reviewed_by_id, isouter=True)
        .where(and_(
            WikiPageDraft.reviewed_at >= win_start,
            WikiPageDraft.reviewed_at < win_end,
            WikiPageDraft.reviewed_by_id.isnot(None),
        ))
        .group_by(WikiPageDraft.reviewed_by_id, Employee.name)
        .order_by(func.count(WikiPageDraft.id).desc())
        .limit(10)
    )).all()
    rows.append({
        "metric_key": "draft.top_reviewers",
        "value_json": {"items": [
            {"reviewer_id": str(rid) if rid else None, "name": name or "(unknown)", "count": int(n)}
            for rid, name, n in top_reviewers
        ]},
    })

    # Compilation plans awaiting review (snapshot)
    plan_pending = (await session.execute(
        select(func.count(SourceCompilationPlan.id)).where(
            SourceCompilationPlan.status == "pending_review"
        )
    )).scalar() or 0
    rows.append({"metric_key": "compile_plan.pending_review", "value_numeric": float(plan_pending)})

    return rows


# ---------------------------------------------------------------------------
# Usage (MCP queries)
# ---------------------------------------------------------------------------

async def _rollup_usage(session: AsyncSession, target_date: date) -> list[dict[str, Any]]:
    win_start, win_end = _window(target_date)
    rows: list[dict[str, Any]] = []

    total = (await session.execute(
        select(func.count(MCPQueryLog.id)).where(
            and_(MCPQueryLog.created_at >= win_start, MCPQueryLog.created_at < win_end)
        )
    )).scalar() or 0
    rows.append({"metric_key": "mcp.queries.total", "value_numeric": float(total)})

    # Zero-result (only meaningful for search-style tools)
    zero = (await session.execute(
        select(func.count(MCPQueryLog.id)).where(and_(
            MCPQueryLog.created_at >= win_start,
            MCPQueryLog.created_at < win_end,
            MCPQueryLog.result_count == 0,
            MCPQueryLog.tool_name.in_(["search_wiki", "list_wiki_pages", "list_sources", "get_knowledge_type_docs"]),
        ))
    )).scalar() or 0
    rows.append({"metric_key": "mcp.queries.zero_result", "value_numeric": float(zero)})

    # Errors / denied
    errors = (await session.execute(
        select(func.count(MCPQueryLog.id)).where(and_(
            MCPQueryLog.created_at >= win_start,
            MCPQueryLog.created_at < win_end,
            MCPQueryLog.status == "error",
        ))
    )).scalar() or 0
    rows.append({"metric_key": "mcp.queries.error", "value_numeric": float(errors)})

    denied = (await session.execute(
        select(func.count(MCPQueryLog.id)).where(and_(
            MCPQueryLog.created_at >= win_start,
            MCPQueryLog.created_at < win_end,
            MCPQueryLog.status == "denied",
        ))
    )).scalar() or 0
    rows.append({"metric_key": "mcp.queries.denied", "value_numeric": float(denied)})

    # DAU
    dau = (await session.execute(
        select(func.count(distinct(MCPQueryLog.employee_id))).where(and_(
            MCPQueryLog.created_at >= win_start,
            MCPQueryLog.created_at < win_end,
            MCPQueryLog.employee_id.isnot(None),
        ))
    )).scalar() or 0
    rows.append({"metric_key": "mcp.active_users", "value_numeric": float(dau)})

    # WAU — distinct users over rolling 7-day window ending at win_end
    wau_start = win_end - timedelta(days=7)
    wau = (await session.execute(
        select(func.count(distinct(MCPQueryLog.employee_id))).where(and_(
            MCPQueryLog.created_at >= wau_start,
            MCPQueryLog.created_at < win_end,
            MCPQueryLog.employee_id.isnot(None),
        ))
    )).scalar() or 0
    rows.append({"metric_key": "mcp.weekly_active_users", "value_numeric": float(wau)})

    # Avg latency
    latency = (await session.execute(
        select(func.avg(MCPQueryLog.latency_ms)).where(and_(
            MCPQueryLog.created_at >= win_start,
            MCPQueryLog.created_at < win_end,
            MCPQueryLog.latency_ms.isnot(None),
        ))
    )).scalar()
    rows.append({"metric_key": "mcp.latency_ms_avg", "value_numeric": float(latency) if latency is not None else None})

    # Distribution by tool
    by_tool = (await session.execute(
        select(MCPQueryLog.tool_name, func.count(MCPQueryLog.id))
        .where(and_(MCPQueryLog.created_at >= win_start, MCPQueryLog.created_at < win_end))
        .group_by(MCPQueryLog.tool_name)
    )).all()
    rows.append({
        "metric_key": "mcp.queries.by_tool",
        "value_json": {"items": [{"tool_name": t, "count": int(n)} for t, n in by_tool]},
    })

    # Top employees
    top_emp = (await session.execute(
        select(
            MCPQueryLog.employee_id,
            Employee.name,
            func.count(MCPQueryLog.id).label("n"),
        )
        .join(Employee, Employee.id == MCPQueryLog.employee_id, isouter=True)
        .where(and_(
            MCPQueryLog.created_at >= win_start,
            MCPQueryLog.created_at < win_end,
            MCPQueryLog.employee_id.isnot(None),
        ))
        .group_by(MCPQueryLog.employee_id, Employee.name)
        .order_by(func.count(MCPQueryLog.id).desc())
        .limit(10)
    )).all()
    rows.append({
        "metric_key": "mcp.top_employees",
        "value_json": {"items": [
            {"employee_id": str(eid) if eid else None, "name": name or "(unknown)", "count": int(n)}
            for eid, name, n in top_emp
        ]},
    })

    return rows


# ---------------------------------------------------------------------------
# Gap analysis — group zero-result queries
# ---------------------------------------------------------------------------

async def _rollup_gaps(session: AsyncSession, target_date: date) -> list[dict[str, Any]]:
    win_start, win_end = _window(target_date)
    result = await session.execute(
        select(
            MCPQueryLog.query_text,
            MCPQueryLog.employee_id,
            MCPQueryLog.tool_name,
        ).where(and_(
            MCPQueryLog.created_at >= win_start,
            MCPQueryLog.created_at < win_end,
            MCPQueryLog.result_count == 0,
            MCPQueryLog.tool_name.in_(["search_wiki", "list_wiki_pages", "list_sources", "get_knowledge_type_docs"]),
            MCPQueryLog.query_text.isnot(None),
        ))
    )
    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "samples": [], "requester_ids": set()})
    for query_text, employee_id, tool_name in result.all():
        key = _norm_text(query_text or "")
        if not key:
            continue
        bucket = grouped[key]
        bucket["count"] += 1
        if len(bucket["samples"]) < 5 and query_text not in bucket["samples"]:
            bucket["samples"].append(query_text)
        if employee_id:
            bucket["requester_ids"].add(str(employee_id))

    items = [
        {
            "normalized": k,
            "count": v["count"],
            "samples": v["samples"],
            "requester_ids": sorted(v["requester_ids"]),
        }
        for k, v in grouped.items()
    ]
    items.sort(key=lambda x: x["count"], reverse=True)
    items = items[:100]

    return [{
        "metric_key": "mcp.gaps.zero_result",
        "value_json": {"items": items},
    }]


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

async def _rollup_audit(session: AsyncSession, target_date: date) -> list[dict[str, Any]]:
    win_start, win_end = _window(target_date)
    rows: list[dict[str, Any]] = []

    denied = (await session.execute(
        select(func.count(AuditLog.id)).where(and_(
            AuditLog.timestamp >= win_start,
            AuditLog.timestamp < win_end,
            AuditLog.decision == "deny",
        ))
    )).scalar() or 0
    rows.append({"metric_key": "audit.denied", "value_numeric": float(denied)})

    by_resource = (await session.execute(
        select(AuditLog.resource_type, func.count(AuditLog.id))
        .where(and_(
            AuditLog.timestamp >= win_start,
            AuditLog.timestamp < win_end,
            AuditLog.decision == "deny",
        ))
        .group_by(AuditLog.resource_type)
    )).all()
    rows.append({
        "metric_key": "audit.denied.by_resource",
        "value_json": {"items": [{"resource_type": rt, "count": int(n)} for rt, n in by_resource]},
    })

    return rows


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_daily_rollup(target_date: date) -> dict[str, int]:
    """Compute and upsert all daily rollup metrics for `target_date` (UTC).

    Returns a dict {section: rows_written} for observability/logging.
    """
    logger.info(f"stats: running daily rollup for {target_date}")
    written: dict[str, int] = {}
    async with async_session_factory() as session:
        for name, fn in [
            ("content", _rollup_content),
            ("contribution", _rollup_contribution),
            ("usage", _rollup_usage),
            ("gaps", _rollup_gaps),
            ("audit", _rollup_audit),
        ]:
            try:
                rows = await fn(session, target_date)
                count = await _upsert_metrics(session, target_date=target_date, rows=rows)
                written[name] = count
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"stats: {name} rollup failed for {target_date}: {exc}")
                written[name] = -1
        await session.commit()
    logger.info(f"stats: rollup complete for {target_date}: {written}")
    return written
