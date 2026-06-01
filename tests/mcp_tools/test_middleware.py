"""
End-to-end-ish tests for ScopedToolsMiddleware.

Strategy: instantiate the real FastMCP server (which registers all tools
via @kb_tool), then monkeypatch `app.mcp.tools._get_identity` to return a
fabricated ResolvedIdentity per scenario, and assert which tool names come
back from `mcp.list_tools()`.

This exercises the middleware end-to-end without needing a database — the
predicate path in `app/mcp/permissions.py` is pure, and we only need to
stub the auth boundary.
"""

import uuid
from typing import Optional

import pytest

from app.mcp.server import create_mcp_server
from app.services.mcp_auth_service import ResolvedIdentity

# Tool tiers — keep this in sync with the @kb_tool decorations in
# app/mcp/tools.py. Used by tests both as ground-truth and as a
# regression detector if someone retiers a tool without updating the
# expected sets here.

PUBLIC_TOOLS = {
    "search_wiki",
    "read_wiki_index",
    "read_wiki_page",
    "list_wiki_pages",
    "get_source",
    "get_source_outline",
    "get_source_pages",
    "list_sources",
    "search_source_content",
    "list_knowledge_types",
    "get_knowledge_type_docs",
}

CONTRIBUTOR_TOOLS = {
    "propose_wiki_edit",
    "propose_wiki_create",
    "resubmit_draft",
    "withdraw_draft",
}

REVIEWER_TOOLS = {
    "list_pending_drafts",
    "review_draft",
    "approve_draft",
    "reject_draft",
    "request_changes_on_draft",
}

DIRECT_WRITE_TOOLS = {
    "edit_wiki_page",
    "create_wiki_page",
}

# `create_wiki_page` / `edit_wiki_page` ride the reviewer ladder
# (see CAN_CREATE_WIKI_DIRECT = CAN_REVIEW_WIKI in app/mcp/permissions.py).
ALL_TOOLS = PUBLIC_TOOLS | CONTRIBUTOR_TOOLS | REVIEWER_TOOLS | DIRECT_WRITE_TOOLS


def _identity(
    *,
    is_admin: bool = False,
    permissions: Optional[list[str]] = None,
) -> ResolvedIdentity:
    return ResolvedIdentity(
        employee_id=uuid.uuid4(),
        employee_name="Test User",
        department_ids=[uuid.uuid4()],
        department_names=["Test Dept"],
        is_admin=is_admin,
        permissions=permissions or [],
    )


def _stub_identity(monkeypatch, identity: Optional[ResolvedIdentity]):
    """Force `_get_identity` to return a fixed value, skipping any DB lookup."""
    async def fake_get_identity():
        return identity, None if identity else "stubbed"

    import app.mcp.tools
    monkeypatch.setattr(app.mcp.tools, "_get_identity", fake_get_identity)


async def _visible(mcp) -> set[str]:
    tools = await mcp.list_tools()
    return {t.name for t in tools}


# ---------------------------------------------------------------------------
# Authenticated scenarios
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_admin_sees_every_tool(monkeypatch):
    mcp = create_mcp_server()
    _stub_identity(monkeypatch, _identity(is_admin=True))
    assert await _visible(mcp) == ALL_TOOLS


@pytest.mark.asyncio
async def test_read_only_employee_sees_public_tools(monkeypatch):
    mcp = create_mcp_server()
    _stub_identity(monkeypatch, _identity(
        permissions=["wiki:read:own_dept", "doc:read:own_dept"],
    ))
    assert await _visible(mcp) == PUBLIC_TOOLS


@pytest.mark.asyncio
async def test_wiki_write_own_dept_sees_contributor_plus_public(monkeypatch):
    mcp = create_mcp_server()
    _stub_identity(monkeypatch, _identity(
        permissions=["wiki:read:own_dept", "wiki:write:own_dept"],
    ))
    assert await _visible(mcp) == PUBLIC_TOOLS | CONTRIBUTOR_TOOLS


@pytest.mark.asyncio
async def test_wiki_write_all_sees_everything_short_of_admin_only(monkeypatch):
    """wiki:write:all is the org-realm reviewer tier — covers reviewer and
    direct-write tools as well."""
    mcp = create_mcp_server()
    _stub_identity(monkeypatch, _identity(
        permissions=["wiki:read:own_dept", "wiki:write:all"],
    ))
    assert await _visible(mcp) == ALL_TOOLS





# ---------------------------------------------------------------------------
# Unauthenticated / invalid token scenario
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unauthenticated_sees_public_tools_with_auth_hint(monkeypatch):
    mcp = create_mcp_server()
    _stub_identity(monkeypatch, None)

    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == PUBLIC_TOOLS, (
        f"Unauthenticated callers must see only the public surface; got {names}"
    )

    for t in tools:
        assert t.description.startswith("[Authenticate to use]"), (
            f"Tool {t.name!r} description should start with the auth hint; "
            f"got: {t.description[:80]!r}"
        )
