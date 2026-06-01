"""
MCP tool-visibility requirements.

Each `@kb_tool` declares a `ToolRequirement` saying which identities can see
the tool in `tools/list`. The `ScopedToolsMiddleware` (app/mcp/middleware.py)
evaluates the predicate against the bearer-token's `ResolvedIdentity` and
hides tools whose predicate returns False.

**Visibility != security.** Predicates here gate the *listing*. Tool bodies
still perform their own per-resource permission checks (e.g. `_can_review_page`
for a specific draft's parent page) because a client can always invoke a tool
by name even if it was hidden in the catalog.

Predicates must be pure functions of `ResolvedIdentity` — no I/O — because
`on_list_tools` fires on every MCP session bootstrap.
"""

from dataclasses import dataclass
from typing import Callable

from app.services.mcp_auth_service import ResolvedIdentity

# Marker attribute set by `kb_tool` on the decorated function.
REQUIRES_ATTR = "__arkon_requires__"


@dataclass(frozen=True)
class ToolRequirement:
    """A pure predicate over ResolvedIdentity, plus a human label.

    The label is surfaced in logs and in the unauthenticated-listing hint so
    operators can tell at a glance why a tool was hidden.
    """
    predicate: Callable[[ResolvedIdentity], bool]
    label: str

    def allows(self, identity: ResolvedIdentity) -> bool:
        return self.predicate(identity)


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

ANY_AUTHENTICATED = ToolRequirement(
    predicate=lambda i: True,
    label="any authenticated identity",
)


def _can_contribute(identity: ResolvedIdentity) -> bool:
    """Author-tier: can propose drafts somewhere, or hold a wiki:write:*."""
    return (
        identity.is_admin
        or identity.has_any_permission("wiki:write:own_dept", "wiki:write:all")
    )


def _can_review(identity: ResolvedIdentity) -> bool:
    """Reviewer-tier: org-wide wiki:write:all."""
    return (
        identity.is_admin
        or identity.has_permission("wiki:write:all")
    )


CAN_CONTRIBUTE_WIKI = ToolRequirement(
    predicate=_can_contribute,
    label="wiki:write:*",
)

CAN_REVIEW_WIKI = ToolRequirement(
    predicate=_can_review,
    label="wiki:write:all",
)

# `create_wiki_page` / `edit_wiki_page` bypass the review queue, so they ride
# the reviewer ladder. Aliased for readability at call sites.
CAN_CREATE_WIKI_DIRECT = CAN_REVIEW_WIKI


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------

def kb_tool(mcp, *, requires: ToolRequirement = ANY_AUTHENTICATED, **fastmcp_kwargs):
    """Replacement for `@mcp.tool()` that attaches a visibility requirement.

    Usage::

        @kb_tool(mcp, requires=CAN_REVIEW_WIKI)
        @logged_tool("approve_draft", query_arg="draft_id")
        async def approve_draft(draft_id: str) -> str: ...

    The requirement is stashed on the function via `REQUIRES_ATTR` so the
    middleware can read it back off the FastMCP `Tool.fn` reference. We then
    delegate to FastMCP's own `mcp.tool(...)` so all native behaviour
    (schema generation, logging, name inference) keeps working.
    """
    def decorator(fn):
        setattr(fn, REQUIRES_ATTR, requires)
        return mcp.tool(**fastmcp_kwargs)(fn)
    return decorator


def requirement_for(fn) -> ToolRequirement:
    """Read the requirement attached by `kb_tool`. Defaults to ANY_AUTHENTICATED.

    Returning a default rather than raising means a tool registered via raw
    `@mcp.tool()` stays *visible* to all authed callers — the CI guard
    (tests/mcp/test_registry_completeness.py) catches the oversight separately.
    """
    return getattr(fn, REQUIRES_ATTR, ANY_AUTHENTICATED)
