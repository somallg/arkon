"""
MCP tool-call logging — captures every tool invocation into `mcp_query_log`
for usage analytics and gap analysis (zero-result queries).

Design:
- `_get_identity()` in tools.py sets `current_identity` ContextVar after resolving
  the bearer token, so the decorator can read employee_id without a 2nd DB call.
- `logged_tool(tool_name, query_arg=...)` wraps each tool body. It measures
  latency, infers status from the returned string, estimates result_count via a
  light heuristic, and persists the log row fire-and-forget.
- Persistence uses its own session and runs as a background task so it never
  blocks the MCP response.
"""

from __future__ import annotations

import asyncio
import functools
import re
import time
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Optional

from loguru import logger


# Set inside _get_identity() once the token resolves to an Employee.
# None when auth failed or the call happened before resolution.
current_identity: ContextVar[Optional[Any]] = ContextVar(
    "mcp_current_identity", default=None
)


_AUTH_FAIL_HINTS = (
    "authentication required",
    "invalid or inactive mcp token",
    "no http request context",
)
_DENIED_HINTS = (
    "access denied",
    "you do not have permission",
    "not allowed",
    "forbidden",
)
_ERROR_HINTS = (
    "error:",
    "failed to",
    "could not",
)

# Patterns that announce a result list — e.g. "**Wiki search — 7 result(s) for: ..."
_COUNT_PATTERNS = (
    re.compile(r"(\d+)\s+result\(s\)"),
    re.compile(r"(\d+)\s+pages?\s+found"),
    re.compile(r"(\d+)\s+sources?\s+found"),
    re.compile(r"(\d+)\s+drafts?\s+pending"),
    re.compile(r"\b(\d+)\s+matches?\b"),
)

_ZERO_RESULT_HINTS = (
    "no wiki pages found",
    "no sources found",
    "no results",
    "no pages found",
    "no drafts pending",
)


def _classify_status(result_text: str) -> str:
    """Infer ok | denied | error from the tool's returned string."""
    if not isinstance(result_text, str):
        return "ok"
    low = result_text.lower()
    for hint in _AUTH_FAIL_HINTS:
        if hint in low:
            return "denied"
    for hint in _DENIED_HINTS:
        if hint in low:
            return "denied"
    # Only flag as error when the message strongly signals failure AND has no
    # successful-search prefix.
    if low.startswith("error") or low.startswith("failed"):
        return "error"
    return "ok"


def _estimate_result_count(result_text: str, tool_name: str) -> Optional[int]:
    """Heuristic: pull a count from the formatted output string."""
    if not isinstance(result_text, str):
        return None
    low = result_text.lower()
    for hint in _ZERO_RESULT_HINTS:
        if hint in low:
            return 0
    for pattern in _COUNT_PATTERNS:
        m = pattern.search(result_text)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                continue
    # Read-style tools that return one page/source — count = 1 on success
    if tool_name in {"read_wiki_page", "get_source", "review_draft"} and _classify_status(result_text) == "ok":
        return 1
    return None


async def _persist_log(
    *,
    tool_name: str,
    employee_id: Optional[Any],
    query_text: Optional[str],
    result_count: Optional[int],
    latency_ms: int,
    status: str,
    error_message: Optional[str],
    scope_metadata: Optional[dict] = None,
) -> None:
    """Write a single mcp_query_log row. Swallows errors so logging never breaks tool calls."""
    try:
        from app.database import async_session_factory
        from app.database.models import MCPQueryLog

        async with async_session_factory() as session:
            row = MCPQueryLog(
                employee_id=employee_id,
                tool_name=tool_name,
                query_text=(query_text[:2000] if query_text else None),
                result_count=result_count,
                latency_ms=latency_ms,
                scope_metadata=scope_metadata,
                status=status,
                error_message=(error_message[:1000] if error_message else None),
            )
            session.add(row)
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Failed to persist MCP query log for {tool_name}: {exc}")


def logged_tool(
    tool_name: str,
    query_arg: Optional[str] = None,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """
    Decorator: wraps an MCP tool function to record a row in `mcp_query_log`.

    Args:
        tool_name: Name to record (use the function name).
        query_arg: Kwarg or first positional arg name to capture as `query_text`
                   (e.g. "query" for search_wiki, "slug" for read_wiki_page).
    """

    def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            start = time.monotonic()
            status = "ok"
            error_message: Optional[str] = None
            result: Any = None
            try:
                result = await fn(*args, **kwargs)
                status = _classify_status(result) if isinstance(result, str) else "ok"
                return result
            except Exception as exc:  # noqa: BLE001
                status = "error"
                error_message = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                latency_ms = int((time.monotonic() - start) * 1000)
                # Pull query text from kwargs first, then first positional arg.
                query_text: Optional[str] = None
                if query_arg:
                    query_text = kwargs.get(query_arg)
                    if query_text is None and args:
                        query_text = args[0] if isinstance(args[0], str) else None
                # Resolve identity from ContextVar set inside _get_identity().
                identity = current_identity.get()
                employee_id = getattr(identity, "employee_id", None) if identity else None
                result_count = _estimate_result_count(result, tool_name) if status == "ok" else None
                asyncio.create_task(
                    _persist_log(
                        tool_name=tool_name,
                        employee_id=employee_id,
                        query_text=(str(query_text) if query_text is not None else None),
                        result_count=result_count,
                        latency_ms=latency_ms,
                        status=status,
                        error_message=error_message,
                    )
                )

        return wrapper

    return decorator
