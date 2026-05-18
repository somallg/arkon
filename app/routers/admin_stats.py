"""
Admin statistics router — serves the /admin/statistics dashboard.

All endpoints read from `stats_daily_rollup` (pre-aggregated by the daily cron
in `app/worker.py:daily_stats_rollup_cron`). For up-to-date numbers, hit
`POST /admin/stats/rollup` to backfill a given date.

All endpoints require `org:settings:manage`. Admins bypass.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.database.models import Employee, StatsDailyRollup
from app.services.auth_service import require_permission
from app.services.stats_aggregator import run_daily_rollup

router = APIRouter(prefix="/admin/stats")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class TimeSeriesPoint(BaseModel):
    date: date
    value: Optional[float] = None
    dimensions: Optional[dict] = None
    items: Optional[list] = None  # for value_json top-N lists


class SectionResponse(BaseModel):
    from_date: date
    to_date: date
    series: dict[str, list[TimeSeriesPoint]]  # metric_key -> points
    latest: dict[str, Optional[float]] = {}  # snapshot of latest scalar values
    latest_lists: dict[str, list] = {}  # latest value_json items


class OverviewResponse(BaseModel):
    as_of: date
    kpis: dict[str, Optional[float]]
    top_gap_topic: Optional[dict] = None
    top_contributor: Optional[dict] = None


class RollupTriggerResponse(BaseModel):
    target_date: date
    sections_written: dict[str, int]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_range(from_q: Optional[str], to_q: Optional[str]) -> tuple[date, date]:
    """Default: last 30 days ending yesterday (UTC)."""
    today = datetime.now(timezone.utc).date()
    to_date = date.fromisoformat(to_q) if to_q else today - timedelta(days=1)
    from_date = date.fromisoformat(from_q) if from_q else to_date - timedelta(days=29)
    if from_date > to_date:
        raise HTTPException(status_code=400, detail="from must be <= to")
    if (to_date - from_date).days > 366:
        raise HTTPException(status_code=400, detail="range cannot exceed 366 days")
    return from_date, to_date


async def _fetch_rows(
    session: AsyncSession,
    metric_keys: list[str],
    from_date: date,
    to_date: date,
) -> list[StatsDailyRollup]:
    win_start = datetime.combine(from_date, datetime.min.time(), tzinfo=timezone.utc)
    win_end = datetime.combine(to_date + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    stmt = select(StatsDailyRollup).where(and_(
        StatsDailyRollup.date >= win_start,
        StatsDailyRollup.date < win_end,
        StatsDailyRollup.metric_key.in_(metric_keys),
    )).order_by(StatsDailyRollup.date.asc())
    return (await session.execute(stmt)).scalars().all()


def _to_section_response(
    rows: list[StatsDailyRollup],
    from_date: date,
    to_date: date,
    scalar_keys: list[str],
    list_keys: list[str],
) -> SectionResponse:
    series: dict[str, list[TimeSeriesPoint]] = {k: [] for k in scalar_keys + list_keys}
    latest: dict[str, Optional[float]] = {}
    latest_lists: dict[str, list] = {}

    for row in rows:
        d = row.date.date() if isinstance(row.date, datetime) else row.date
        point = TimeSeriesPoint(
            date=d,
            value=row.value_numeric,
            dimensions=row.dimensions,
            items=(row.value_json or {}).get("items") if row.value_json else None,
        )
        series.setdefault(row.metric_key, []).append(point)
        if row.metric_key in scalar_keys and row.value_numeric is not None:
            latest[row.metric_key] = row.value_numeric  # rows are date-ordered ASC, last wins
        if row.metric_key in list_keys and row.value_json:
            latest_lists[row.metric_key] = row.value_json.get("items", [])

    return SectionResponse(
        from_date=from_date,
        to_date=to_date,
        series=series,
        latest=latest,
        latest_lists=latest_lists,
    )


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

OVERVIEW_KPIS = [
    "wiki.pages.total",
    "wiki.pages.stale_30d",
    "wiki.pages.orphan",
    "wiki.pages.updated",
    "draft.pending",
    "draft.time_to_review_avg_seconds",
    "compile_plan.pending_review",
    "mcp.active_users",
    "mcp.weekly_active_users",
    "mcp.queries.total",
    "mcp.queries.zero_result",
    "audit.denied",
]


@router.get("/overview", response_model=OverviewResponse)
async def get_overview(
    db: AsyncSession = Depends(get_db),
    _user: Employee = require_permission("org:settings:manage"),
):
    """Latest snapshot of headline KPIs + top gap topic + top contributor (last 30 days)."""
    today = datetime.now(timezone.utc).date()
    from_date = today - timedelta(days=30)
    to_date = today

    rows = await _fetch_rows(db, OVERVIEW_KPIS, from_date, to_date)
    kpis: dict[str, Optional[float]] = {k: None for k in OVERVIEW_KPIS}
    for row in rows:
        if row.value_numeric is not None:
            # ASC ordering by date — last assignment wins
            kpis[row.metric_key] = row.value_numeric

    # Top gap topic — most recent gap rollup with items, take #1
    gap_rows = await _fetch_rows(db, ["mcp.gaps.zero_result"], from_date, to_date)
    top_gap: Optional[dict] = None
    if gap_rows:
        latest_gap = gap_rows[-1]
        items = (latest_gap.value_json or {}).get("items", [])
        if items:
            top_gap = items[0]

    contributor_rows = await _fetch_rows(db, ["draft.top_contributors"], from_date, to_date)
    top_contributor: Optional[dict] = None
    if contributor_rows:
        latest_c = contributor_rows[-1]
        items = (latest_c.value_json or {}).get("items", [])
        if items:
            top_contributor = items[0]

    return OverviewResponse(
        as_of=to_date,
        kpis=kpis,
        top_gap_topic=top_gap,
        top_contributor=top_contributor,
    )


# ---------------------------------------------------------------------------
# Per-section endpoints
# ---------------------------------------------------------------------------

CONTENT_SCALAR = [
    "wiki.pages.total",
    "wiki.pages.created",
    "wiki.pages.updated",
    "wiki.pages.stale_30d",
    "wiki.pages.orphan",
    "wiki.revisions.daily",
]
CONTENT_LIST = ["wiki.pages.by_type", "wiki.top_pages"]

CONTRIBUTION_SCALAR = [
    "draft.created",
    "draft.approved",
    "draft.rejected",
    "draft.pending",
    "draft.time_to_review_avg_seconds",
    "compile_plan.pending_review",
]
CONTRIBUTION_LIST = ["draft.top_contributors", "draft.top_reviewers", "draft.created.by_source"]

USAGE_SCALAR = [
    "mcp.queries.total",
    "mcp.queries.zero_result",
    "mcp.queries.error",
    "mcp.queries.denied",
    "mcp.active_users",
    "mcp.weekly_active_users",
    "mcp.latency_ms_avg",
]
USAGE_LIST = ["mcp.queries.by_tool", "mcp.top_employees"]

GAP_LIST = ["mcp.gaps.zero_result"]


@router.get("/content", response_model=SectionResponse)
async def get_content(
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    db: AsyncSession = Depends(get_db),
    _user: Employee = require_permission("org:settings:manage"),
):
    f, t = _parse_range(from_date, to_date)
    rows = await _fetch_rows(db, CONTENT_SCALAR + CONTENT_LIST, f, t)
    return _to_section_response(rows, f, t, CONTENT_SCALAR, CONTENT_LIST)


@router.get("/contribution", response_model=SectionResponse)
async def get_contribution(
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    db: AsyncSession = Depends(get_db),
    _user: Employee = require_permission("org:settings:manage"),
):
    f, t = _parse_range(from_date, to_date)
    rows = await _fetch_rows(db, CONTRIBUTION_SCALAR + CONTRIBUTION_LIST, f, t)
    return _to_section_response(rows, f, t, CONTRIBUTION_SCALAR, CONTRIBUTION_LIST)


@router.get("/usage", response_model=SectionResponse)
async def get_usage(
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    db: AsyncSession = Depends(get_db),
    _user: Employee = require_permission("org:settings:manage"),
):
    f, t = _parse_range(from_date, to_date)
    rows = await _fetch_rows(db, USAGE_SCALAR + USAGE_LIST, f, t)
    return _to_section_response(rows, f, t, USAGE_SCALAR, USAGE_LIST)


@router.get("/gaps")
async def get_gaps(
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _user: Employee = require_permission("org:settings:manage"),
):
    """Top zero-result MCP queries grouped by normalized text, aggregated over the window."""
    f, t = _parse_range(from_date, to_date)
    rows = await _fetch_rows(db, GAP_LIST, f, t)

    # Merge daily rollups: sum counts per normalized key, union samples & requesters.
    merged: dict[str, dict] = {}
    for row in rows:
        if not row.value_json:
            continue
        for item in row.value_json.get("items", []):
            key = item.get("normalized")
            if not key:
                continue
            bucket = merged.setdefault(key, {
                "normalized": key,
                "count": 0,
                "samples": [],
                "requester_ids": set(),
            })
            bucket["count"] += int(item.get("count", 0))
            for s in item.get("samples", []):
                if s not in bucket["samples"] and len(bucket["samples"]) < 5:
                    bucket["samples"].append(s)
            for rid in item.get("requester_ids", []) or []:
                bucket["requester_ids"].add(rid)

    items = [
        {**v, "requester_ids": sorted(v["requester_ids"])}
        for v in merged.values()
    ]
    items.sort(key=lambda x: x["count"], reverse=True)
    return {"from_date": f, "to_date": t, "items": items[:limit]}


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

_SECTIONS = {
    "content": CONTENT_SCALAR + CONTENT_LIST,
    "contribution": CONTRIBUTION_SCALAR + CONTRIBUTION_LIST,
    "usage": USAGE_SCALAR + USAGE_LIST,
    "gaps": GAP_LIST,
}


@router.get("/export/{section}.csv")
async def export_section(
    section: str,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    db: AsyncSession = Depends(get_db),
    _user: Employee = require_permission("org:settings:manage"),
):
    if section not in _SECTIONS:
        raise HTTPException(status_code=404, detail=f"unknown section: {section}")
    f, t = _parse_range(from_date, to_date)
    rows = await _fetch_rows(db, _SECTIONS[section], f, t)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["date", "metric_key", "dimensions_json", "value_numeric", "value_json"])
    for row in rows:
        d = row.date.date().isoformat() if isinstance(row.date, datetime) else str(row.date)
        import json as _json
        writer.writerow([
            d,
            row.metric_key,
            _json.dumps(row.dimensions) if row.dimensions else "",
            row.value_numeric if row.value_numeric is not None else "",
            _json.dumps(row.value_json) if row.value_json else "",
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=stats_{section}_{f}_{t}.csv"},
    )


# ---------------------------------------------------------------------------
# Backfill / manual trigger
# ---------------------------------------------------------------------------

@router.post("/rollup", response_model=RollupTriggerResponse)
async def trigger_rollup(
    target: Optional[str] = Query(None, description="ISO date to rollup; default = yesterday UTC"),
    _user: Employee = require_permission("org:settings:manage"),
):
    """Run (or re-run) the daily rollup for one date. Idempotent."""
    if target:
        try:
            target_date = date.fromisoformat(target)
        except ValueError:
            raise HTTPException(status_code=400, detail="target must be ISO date YYYY-MM-DD")
    else:
        target_date = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    result = await run_daily_rollup(target_date)
    return RollupTriggerResponse(target_date=target_date, sections_written=result)
