"""
Arkon MCP Tools — LLM Wiki + raw source drill-down for Claude.

The wiki layer is the primary surface: Claude searches and reads markdown
pages compiled from sources. Raw-source tools (PageIndex-style) act as a
fallback for precise citations or text the wiki has paraphrased away.

All tools verify the employee's MCP token and enforce knowledge_type scope:
  - search_wiki / read_wiki_page / list_wiki_pages: filter by
    `knowledge_type_slugs && allowed_knowledge_types` (Postgres ARRAY overlap).
  - get_source / get_source_outline / get_source_pages / list_sources /
    get_knowledge_type_docs: enforce per-source scope via apply_scope_filter.
"""

from typing import Optional

from fastmcp import FastMCP
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp.logging import current_identity, logged_tool
from app.mcp.permissions import (
    ANY_AUTHENTICATED,
    CAN_CONTRIBUTE_WIKI,
    CAN_CREATE_WIKI_DIRECT,
    CAN_REVIEW_WIKI,
    kb_tool,
)

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

async def _get_identity():
    """Resolve the bearer token to a ResolvedIdentity, or return an error string."""
    from fastmcp.server.dependencies import get_http_request

    from app.database import async_session_factory
    from app.services.mcp_auth_service import MCPAuthService

    try:
        request = get_http_request()
        auth_header = request.headers.get("authorization", "")
        token = auth_header.removeprefix("Bearer ").strip()
    except RuntimeError:
        return None, "No HTTP request context available."

    if not token:
        return None, (
            "Authentication required. Configure your MCP token in Claude Desktop:\n"
            '{"mcpServers": {"arkon": {"url": "...", '
            '"headers": {"Authorization": "Bearer <your-token>"}}}}'
        )

    async with async_session_factory() as session:
        auth_svc = MCPAuthService(session)
        identity = await auth_svc.verify_token(token)
        if identity is None:
            return None, "Invalid or inactive MCP token. Contact your administrator."
        # Only commit when verify_token actually bumped last_connected;
        # otherwise this is a pure read and an empty COMMIT round-trips Redis
        # latency for nothing on every MCP tool call.
        if auth_svc.bumped_last_connected:
            await session.commit()

    current_identity.set(identity)
    return identity, None


async def _get_allowed_source_ids(identity, session: Optional[AsyncSession] = None) -> Optional[set[str]]:
    """Allowed source UUID strings, or None when access is unrestricted.

    Pass an existing session to avoid opening a second DB connection.
    """
    if identity.is_admin:
        return None
    if identity.allowed_source_ids is None and identity.allowed_knowledge_types is None:
        return None

    from sqlalchemy import select

    from app.database import async_session_factory
    from app.database.models import Source
    from app.services.mcp_auth_service import apply_scope_filter

    async def _query(s: AsyncSession) -> set[str]:
        stmt = select(Source.id).where(Source.status == "ready")
        stmt = apply_scope_filter(stmt, identity)
        result = await s.execute(stmt)
        return {str(r[0]) for r in result.all()}

    if session is not None:
        return await _query(session)

    async with async_session_factory() as session:
        return await _query(session)


# ---------------------------------------------------------------------------
# Permission helpers (shared across review/contribute tools)
# ---------------------------------------------------------------------------

async def _can_review_page(session: AsyncSession, employee, page) -> bool:
    """Editor+ in the page's workspace, or wiki:write:all globally, or admin."""
    from app.services.permission_engine import (
        _get_user_permissions,
        get_workspace_role,
        workspace_role_can,
    )
    if employee.role == "admin":
        return True
    if page.scope_type == "project" and page.scope_id:
        role = await get_workspace_role(session, employee, page.scope_id)
        return bool(role) and workspace_role_can(role, "editor")
    perms = _get_user_permissions(employee)
    return "wiki:write:all" in perms


async def _can_contribute_to_page(session: AsyncSession, employee, page) -> bool:
    """Permission to propose an edit on `page` via MCP.

    Mirrors REST `_can_propose`:
    - Project pages: workspace contributor+.
    - Department pages: wiki:write:all, or wiki:write:own_dept restricted to
      the employee's own department.
    - Global pages: any wiki:write permission.
    """
    from app.services.permission_engine import (
        _get_user_permissions,
        get_workspace_role,
        has_any_permission,
        workspace_role_can,
    )
    if employee.role == "admin":
        return True
    perms = _get_user_permissions(employee)
    if page.scope_type == "project" and page.scope_id:
        role = await get_workspace_role(session, employee, page.scope_id)
        if not role:
            return False
        return workspace_role_can(role, "contributor")
    if page.scope_type == "department" and page.scope_id:
        if "wiki:write:all" in perms:
            return True
        return (
            "wiki:write:own_dept" in perms
            and page.scope_id in employee.department_ids
        )
    return has_any_permission(list(perms), "wiki", "write")


# ---------------------------------------------------------------------------
# Out-of-scope hint (Tier 1 — count + scope name, no titles/content leaked)
# ---------------------------------------------------------------------------

async def _format_oos_hint(session: AsyncSession, oos_hits: list) -> str:
    """Aggregate out-of-scope search hits into a short "ask for access" hint.

    Intentionally leaks ONLY (count, scope_type, scope_name) — never titles
    or summaries — to avoid information disclosure across department or
    workspace boundaries. A page title can itself be sensitive
    (e.g. "Q1 layoffs — Engineering").
    """
    if not oos_hits:
        return ""

    from collections import Counter

    from app.database.models import Department

    # Group by (scope_type, scope_id) → count.
    buckets: Counter[tuple[str, str | None]] = Counter()
    for page, _sim in oos_hits:
        scope_type = page.scope_type or "global"
        if scope_type == "global":
            continue  # global pages should already be visible; defensive skip
        scope_id = str(page.scope_id) if page.scope_id else None
        buckets[(scope_type, scope_id)] += 1

    if not buckets:
        return ""

    # Resolve human-readable scope labels.
    labels: dict[tuple[str, str | None], str] = {}
    for (scope_type, scope_id) in buckets.keys():
        label: str | None = None
        if scope_id:
            import uuid as _uuid
            try:
                sid = _uuid.UUID(scope_id)
            except (ValueError, TypeError):
                sid = None
            if sid is not None:
                if scope_type == "department":
                    d = await session.get(Department, sid)
                    label = d.name if d else None
        labels[(scope_type, scope_id)] = label or "(unknown)"

    lines = ["**Out-of-scope matches** — matching page(s) exist outside your access:"]
    for (scope_type, scope_id), count in buckets.most_common():
        label = labels[(scope_type, scope_id)]
        if scope_type == "department":
            lines.append(
                f"- {count} page(s) in department **{label}** — "
                f"contact the {label} department admin to request access."
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def register_tools(mcp: FastMCP):
    """Register all KB tools on the MCP server."""

    # =========================================================================
    # Wiki layer — synthesized markdown pages compiled from sources
    # =========================================================================

    @kb_tool(mcp, requires=ANY_AUTHENTICATED)
    @logged_tool("search_wiki", query_arg="query")
    async def search_wiki(query: str, top_k: int = 10) -> str:
        """
        Semantic search across the knowledge base — synthesized wiki pages AND
        verbatim source documents — in one ranked list.

        Use this FIRST when answering a question about the organization. Results
        are of two kinds:
          - 📘 Wiki pages: persistent, interlinked summaries compiled from many
            sources. Read the full page with `read_wiki_page(slug)`.
          - 📄 Source matches: passages from high-fidelity documents kept verbatim
            (decrees, gazettes, contracts) that are NOT rewritten. Read the exact
            original text with `get_source_pages(source_id, "<page>")` and cite the
            portal link shown so the user can open or download the original.

        If the response includes an "Out-of-scope matches" section, pages matching
        the query exist but live in a department or workspace the caller is not a
        member of — tell the user to request access instead of assuming it's missing.

        Args:
            query: Natural language search query.
            top_k: Maximum number of results to return (default: 10, max: 50).

        Returns:
            A ranked list of wiki pages and source matches with similarity scores.
        """
        import uuid as uuid_mod

        identity, err = await _get_identity()
        if err:
            return err
        assert identity is not None

        top_k = min(max(1, top_k), 50)

        from app.config import settings
        from app.ai.registry import ProviderRegistry
        from app.database import async_session_factory
        from app.services import wiki_service

        proj_uuids = [uuid_mod.UUID(p) for p in identity.project_ids] or None

        async with async_session_factory() as session:
            registry = ProviderRegistry(session)
            embedding_provider = await registry.get_embedding(task="search_query")
            query_embedding = await embedding_provider.embed(query)

            hits = await wiki_service.search_pages_semantic(
                session,
                query_embedding=query_embedding,
                top_k=top_k,
                allowed_kt_slugs=identity.allowed_knowledge_types,
                department_ids=identity.department_ids,
                project_ids=proj_uuids,
                all_scopes=identity.is_admin,
            )

            # Verbatim source chunks — same semantic pool, RBAC-scoped to the
            # caller's allowed source set (mirrors the source drill-down tools).
            allowed_source_ids = await _get_allowed_source_ids(identity, session)
            chunk_hits = await wiki_service.search_source_chunks_semantic(
                session,
                query_embedding=query_embedding,
                top_k=top_k,
                allowed_source_ids=allowed_source_ids,
            )

            # Out-of-scope peek — admins already see everything, so the hint
            # only fires for non-admins. Limit to a small fixed sample so an
            # adversary can't enumerate the entire org's page list via search.
            oos_hint = ""
            if not identity.is_admin:
                oos_hits = await wiki_service.search_pages_semantic(
                    session,
                    query_embedding=query_embedding,
                    top_k=5,
                    department_ids=identity.department_ids,
                    project_ids=proj_uuids,
                    inverse_scope=True,
                )
                oos_hint = await _format_oos_hint(session, oos_hits)

        # Collapse multiple chunk hits from the same source into one entry
        # (best similarity + the matching page numbers).
        grouped: dict = {}
        for source, chunk, sim in chunk_hits:
            g = grouped.get(source.id)
            if g is None or sim > g["sim"]:
                preview = (chunk.text or "").strip().replace("\n", " ")
                if len(preview) > 200:
                    preview = preview[:200] + "…"
                grouped[source.id] = {
                    "source": source, "sim": sim, "preview": preview, "pages": set(),
                }
            grouped[source.id]["pages"].add(chunk.page_number)

        # Unify into one ranked list by similarity.
        ranked: list = [("wiki", sim, page) for page, sim in hits]
        ranked += [("source", g["sim"], g) for g in grouped.values()]
        ranked.sort(key=lambda r: r[1], reverse=True)
        ranked = ranked[:top_k]

        if not ranked:
            base = f"No knowledge base matches found for: \"{query}\""
            if oos_hint:
                return f"{base}\n\n{oos_hint}"
            return base

        def _portal_link(source_id) -> str:
            base = (settings.portal_base_url or "").rstrip("/")
            return f"{base}/wiki/source/{source_id}" if base else f"/wiki/source/{source_id}"

        lines = [f"**KB search — {len(ranked)} result(s) for: \"{query}\"**\n"]
        for kind, sim, obj in ranked:
            similarity_pct = f"{sim:.0%}"
            if kind == "wiki":
                page = obj
                kt_label = (
                    f" [{', '.join(page.knowledge_type_slugs)}]"
                    if page.knowledge_type_slugs else ""
                )
                entry = (
                    f"- 📘 `{page.slug}` ({page.page_type}){kt_label} — {similarity_pct}\n"
                    f"  **{page.title}**"
                )
                if page.summary:
                    entry += f" — {page.summary}"
                entry += "\n  _Read: `read_wiki_page(\"%s\")`_" % page.slug
            else:
                source = obj["source"]
                title = source.title or source.file_name or source.url or "Untitled"
                pages_sorted = sorted(obj["pages"])
                pages_label = ",".join(str(p) for p in pages_sorted[:5])
                entry = (
                    f"- 📄 **{title}** (nguyên văn) — {similarity_pct} — trang {pages_label}\n"
                    f"  “{obj['preview']}”\n"
                    f"  _Đọc bản gốc: `get_source_pages(\"{source.id}\", \"{pages_sorted[0]}\")` · "
                    f"Link: {_portal_link(source.id)}_"
                )
            lines.append(entry)

        if oos_hint:
            lines.append("")
            lines.append(oos_hint)
        return "\n".join(lines)

    @kb_tool(mcp, requires=ANY_AUTHENTICATED)
    @logged_tool("read_wiki_index")
    async def read_wiki_index() -> str:
        """
        Read the wiki catalog (`_index` page).

        The index lists every wiki page grouped by type, with one-line
        summaries. Use this to discover the shape of the wiki before drilling
        into specific pages.
        """
        identity, err = await _get_identity()
        if err:
            return err
        assert identity is not None

        from app.database import async_session_factory
        from app.services import wiki_service

        async with async_session_factory() as session:
            page = await wiki_service.get_page_by_slug(session, wiki_service.INDEX_SLUG)

        if not page:
            return "_(wiki index not initialized yet)_"
        return page.content_md

    @kb_tool(mcp, requires=ANY_AUTHENTICATED)
    @logged_tool("read_wiki_page", query_arg="slug")
    async def read_wiki_page(slug: str) -> str:
        """
        Read a specific wiki page by slug, plus its backlinks.

        Args:
            slug: The page slug, e.g. "entity/jane-doe", "concept/onboarding".
                  Use `search_wiki` or `list_wiki_pages` to find slugs.

        Returns:
            Markdown content of the page, plus an "Outlinks" section listing
            pages this one links out to, and a "Backlinks" section listing pages
            that link back to it. Both lists (and any inline `[[slug]]` wikilinks)
            can be followed with another `read_wiki_page` call to traverse the
            knowledge graph.
        """
        identity, err = await _get_identity()
        if err:
            return err
        assert identity is not None

        from app.database import async_session_factory
        from app.services import wiki_service

        import uuid as uuid_mod
        from sqlalchemy import select as sa_select

        from app.database.models import Department, WikiPage

        async with async_session_factory() as session:
            # Try global → department.
            page = await wiki_service.get_page_by_slug(
                session, slug, allowed_kt_slugs=identity.allowed_knowledge_types,
            )
            if not page and identity.department_ids:
                # Walk the user's departments until we hit a matching slug.
                for did in identity.department_ids:
                    page = await wiki_service.get_page_by_slug(
                        session, slug,
                        allowed_kt_slugs=identity.allowed_knowledge_types,
                        scope_type="department",
                        scope_id=did,
                    )
                    if page:
                        break

            if not page:
                # Out-of-scope hint: does the slug exist in a scope the caller
                # CAN'T access? If so, leak only the scope label (no content).
                if not identity.is_admin:
                    stmt = sa_select(WikiPage).where(WikiPage.slug == slug)
                    others = (await session.execute(stmt)).scalars().all()
                    inaccessible = [
                        p for p in others
                        if p.scope_type == "department" and p.scope_id not in identity.department_ids
                    ]
                    if inaccessible:
                        labels: list[str] = []
                        for p in inaccessible:
                            if p.scope_type == "department" and p.scope_id:
                                d = await session.get(Department, p.scope_id)
                                labels.append(
                                    f"department **{d.name if d else '(unknown)'}**"
                                )
                        # Dedup while preserving order.
                        seen: set[str] = set()
                        unique_labels = [
                            x for x in labels if not (x in seen or seen.add(x))
                        ]
                        joined = ", ".join(unique_labels)
                        return (
                            f"Wiki page `{slug}` exists in {joined} but you don't "
                            f"have access. Contact the scope's admin to request access."
                        )
                return f"Wiki page not found or out of scope: `{slug}`"

            backlinks = await wiki_service.get_backlinks(
                session, slug, page.scope_type, page.scope_id,
            )
            outlinks = await wiki_service.get_outlinks(
                session, slug, page.scope_type, page.scope_id,
            )

        body = page.content_md or ""
        outlinks = sorted({s for s in outlinks if s != slug})
        if outlinks:
            body = body.rstrip() + "\n\n## Outlinks\n" + "\n".join(
                f"- `{s}`" for s in outlinks
            )
        if backlinks:
            body = body.rstrip() + "\n\n## Backlinks\n" + "\n".join(
                f"- `{s}`" for s in sorted(backlinks)
            )
        return body

    @kb_tool(mcp, requires=ANY_AUTHENTICATED)
    @logged_tool("list_wiki_pages")
    async def list_wiki_pages(
        page_type: Optional[str] = None,
        knowledge_type: Optional[str] = None,
        query: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> str:
        """
        Browse wiki pages with filters and text search. Reserved pages (`_index`, `_log`) are excluded.

        Args:
            page_type: Filter by type — "entity", "concept", "topic", "source".
            knowledge_type: Filter by KnowledgeType slug.
            query: Optional substring search query (case-insensitive) matched against title, slug, and content.
            limit: Max pages to return (default: 50).
            offset: Number of pages to skip for pagination (default: 0).

        Returns:
            Slug, title, summary, type, and KnowledgeType slugs for each page.
        """
        import uuid as uuid_mod

        identity, err = await _get_identity()
        if err:
            return err
        assert identity is not None

        from app.database import async_session_factory
        from app.services import wiki_service

        proj_uuids = [uuid_mod.UUID(p) for p in identity.project_ids] or None

        async with async_session_factory() as session:
            pages = await wiki_service.list_pages(
                session,
                page_type=page_type,
                knowledge_type_slug=knowledge_type,
                allowed_kt_slugs=identity.allowed_knowledge_types,
                query=query,
                limit=limit,
                offset=offset,
                department_ids=identity.department_ids,
                project_ids=proj_uuids,
                all_scopes=identity.is_admin,
            )

        if not pages:
            return "No wiki pages match the filters."

        lines = [f"**Wiki pages — {len(pages)} result(s)**\n"]
        for p in pages:
            kt_label = f" [{', '.join(p.knowledge_type_slugs)}]" if p.knowledge_type_slugs else ""
            line = f"- `{p.slug}` ({p.page_type}){kt_label} — **{p.title}**"
            if p.summary:
                line += f" — {p.summary}"
            lines.append(line)
        return "\n".join(lines)

    # =========================================================================
    # Raw source drill-down (PageIndex-inspired)
    # =========================================================================

    @kb_tool(mcp, requires=ANY_AUTHENTICATED)
    @logged_tool("get_source", query_arg="source_id")
    async def get_source(source_id: str) -> str:
        """
        Metadata for a raw source document — title, knowledge type, page count,
        contributor, status. Use this before reading source pages.

        Args:
            source_id: The source UUID.
        """
        import uuid as uuid_mod

        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from app.database import async_session_factory
        from app.database.models import Source

        identity, err = await _get_identity()
        if err:
            return err
        assert identity is not None
        try:
            sid = uuid_mod.UUID(source_id)
        except ValueError:
            return f"Invalid source ID: {source_id}"

        async with async_session_factory() as session:
            stmt = (
                select(Source).where(Source.id == sid)
                .options(selectinload(Source.knowledge_type), selectinload(Source.contributor))
            )
            source = (await session.execute(stmt)).scalar_one_or_none()
            if not source:
                return f"Source not found: {source_id}"

            allowed_ids = await _get_allowed_source_ids(identity, session)
            if allowed_ids is not None and str(sid) not in allowed_ids:
                return "Access denied: this source is outside your knowledge scope."

        page_count = len(source.page_offsets or [])
        kt_label = source.knowledge_type.name if source.knowledge_type else "Uncategorized"
        contributor_label = source.contributor.name if source.contributor else "(admin upload)"

        lines = [
            f"# {source.title or source.file_name or 'Untitled Source'}",
            f"- **ID:** `{source.id}`",
            f"- **Type:** {source.source_type or 'file'}",
            f"- **Knowledge type:** {kt_label}",
            f"- **Status:** {source.status}",
            f"- **Pages:** {page_count}" if page_count else "- **Pages:** (single block)",
            f"- **Contributed by:** {contributor_label}",
        ]
        if source.file_name:
            lines.append(f"- **File:** {source.file_name}")
        if source.url:
            lines.append(f"- **URL:** {source.url}")
        if source.created_at:
            lines.append(f"- **Added:** {source.created_at.strftime('%Y-%m-%d %H:%M')}")
        return "\n".join(lines)

    @kb_tool(mcp, requires=ANY_AUTHENTICATED)
    @logged_tool("get_source_outline", query_arg="source_id")
    async def get_source_outline(source_id: str) -> str:
        """
        Heading-based outline (table of contents) of a raw source.

        Use this to navigate long documents by structure instead of guessing
        page numbers. Each entry shows title, level, page, and char range.
        Pass char_start/char_end downstream is not needed — use the page
        number with `get_source_pages` for the actual text.

        Args:
            source_id: The source UUID.
        """
        import uuid as uuid_mod

        from app.database import async_session_factory
        from app.database.models import Source

        identity, err = await _get_identity()
        if err:
            return err
        assert identity is not None
        try:
            sid = uuid_mod.UUID(source_id)
        except ValueError:
            return f"Invalid source ID: {source_id}"

        async with async_session_factory() as session:
            source = await session.get(Source, sid)
            if not source:
                return f"Source not found: {source_id}"
            allowed_ids = await _get_allowed_source_ids(identity, session)
            if allowed_ids is not None and str(sid) not in allowed_ids:
                return "Access denied: this source is outside your knowledge scope."

        outline = source.outline_json or []
        if not outline:
            return "_(no outline — this document has no detectable headings)_"

        lines = ["# Outline\n"]
        def _walk(nodes: list[dict]):
            for n in nodes:
                indent = "  " * (max(0, n.get("level", 1) - 1))
                page = n.get("page")
                page_label = f" (page {page})" if page else ""
                lines.append(f"{indent}- {n.get('title', '')}{page_label}")
                if n.get("children"):
                    _walk(n["children"])
        _walk(outline)
        return "\n".join(lines)

    @kb_tool(mcp, requires=ANY_AUTHENTICATED)
    @logged_tool("get_source_pages", query_arg="source_id")
    async def get_source_pages(source_id: str, pages: str) -> str:
        """
        Read raw text of specific pages from a source.

        Args:
            source_id: The source UUID.
            pages: Page range — examples: "5-7", "3,8", "12", "1-3,9".

        Returns:
            Concatenated page text with `--- page N ---` separators. Use this
            for precise citations when the wiki summary has paraphrased away
            details you need.
        """
        import uuid as uuid_mod

        from app.database import async_session_factory
        from app.database.models import Source
        from app.services.source_outline import parse_page_range, slice_pages_by_range

        identity, err = await _get_identity()
        if err:
            return err
        assert identity is not None
        try:
            sid = uuid_mod.UUID(source_id)
        except ValueError:
            return f"Invalid source ID: {source_id}"

        page_nums = parse_page_range(pages)
        if not page_nums:
            return f"Invalid page range: {pages!r}. Use formats like '5-7', '3,8', '12'."

        async with async_session_factory() as session:
            source = await session.get(Source, sid)
            if not source:
                return f"Source not found: {source_id}"
            allowed_ids = await _get_allowed_source_ids(identity, session)
            if allowed_ids is not None and str(sid) not in allowed_ids:
                return "Access denied: this source is outside your knowledge scope."

        full_text = source.full_text or ""
        offsets = source.page_offsets or []
        if not full_text or not offsets:
            return "_(no extractable text or page offsets for this source)_"

        slices = slice_pages_by_range(full_text, offsets, page_nums)
        if not slices:
            return f"No content for pages: {page_nums}"

        parts = []
        for s in slices:
            parts.append(f"--- page {s['page']} ---\n{s['content']}")
        return "\n\n".join(parts)

    @kb_tool(mcp, requires=ANY_AUTHENTICATED)
    @logged_tool("search_source_content", query_arg="query")
    async def search_source_content(
        query: str,
        limit: int = 10,
        offset: int = 0,
    ) -> str:
        """
        Search inside the full text of raw source documents.

        Use this tool when the wiki summarizes or paraphases too much and you
        need to search across all raw text documents to locate specific terms,
        clauses, product codes, or quotes.

        Args:
            query: Exact keyword or phrase to search for in document contents.
            limit: Max sources to return matches for (default: 10).
            offset: Number of sources to skip for pagination (default: 0).

        Returns:
            Matched source documents, their matching page numbers, and highlighted context snippets.
        """
        import re
        from sqlalchemy import select

        from app.database import async_session_factory
        from app.database.models import Source
        from app.services.mcp_auth_service import apply_scope_filter

        identity, err = await _get_identity()
        if err:
            return err
        assert identity is not None

        query = query.strip()
        if not query:
            return "Error: query parameter must not be empty."

        async with async_session_factory() as session:
            stmt = select(Source).where(
                Source.status == "ready",
                Source.full_text.ilike(f"%{query}%")
            )
            stmt = apply_scope_filter(stmt, identity).offset(offset).limit(limit)
            sources = (await session.execute(stmt)).scalars().all()

        if not sources:
            return f"No document content matches found for: \"{query}\""

        try:
            pattern = re.compile(re.escape(query), re.IGNORECASE)
        except Exception:
            pattern = None

        lines = [f"**Content search results for: \"{query}\"**\n"]
        for s in sources:
            text = s.full_text or ""
            offsets = s.page_offsets or []
            title = s.title or s.file_name or s.url or "Untitled Source"

            matches_in_doc = []
            if pattern:
                for match in pattern.finditer(text):
                    start_char = match.start()
                    page_num = 1
                    if offsets:
                        for idx, off in enumerate(offsets):
                            if start_char < off:
                                page_num = idx
                                break
                            page_num = len(offsets)

                    start_window = max(0, start_char - 80)
                    end_window = min(len(text), match.end() + 80)
                    snippet = text[start_window:end_window].strip().replace("\n", " ")
                    if start_window > 0:
                        snippet = "..." + snippet
                    if end_window < len(text):
                        snippet = snippet + "..."

                    # Highlight the query using bold Markdown
                    highlighted_snippet = re.sub(
                        re.escape(query),
                        lambda m: f"**{m.group(0)}**",
                        snippet,
                        flags=re.IGNORECASE
                    )

                    matches_in_doc.append((page_num, highlighted_snippet))
                    if len(matches_in_doc) >= 3:
                        break

            lines.append(f"### {title} (ID: `{s.id}`)")
            if matches_in_doc:
                for p_num, snip in matches_in_doc:
                    lines.append(f"- **Page {p_num}**: {snip}")
            else:
                lines.append("- Keyword matches found in full text.")
            lines.append("")

        return "\n".join(lines)

    # =========================================================================
    # Source/Type browsing
    # =========================================================================

    @kb_tool(mcp, requires=ANY_AUTHENTICATED)
    @logged_tool("list_sources")
    async def list_sources(
        status: str = "ready",
        knowledge_type: Optional[str] = None,
        query: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> str:
        """
        List raw source documents with optional filters and text search.

        Args:
            status: "ready", "processing", "error", or "all".
            knowledge_type: Filter by KnowledgeType slug.
            query: Optional text query (case-insensitive) to search in title, filename, or URL.
            limit: Max sources to return (default: 20).
            offset: Number of sources to skip for pagination (default: 0).
        """
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from app.database import async_session_factory
        from app.database.models import KnowledgeType, Source
        from app.services.mcp_auth_service import apply_scope_filter

        identity, err = await _get_identity()
        if err:
            return err
        assert identity is not None

        async with async_session_factory() as session:
            stmt = (
                select(Source)
                .options(selectinload(Source.knowledge_type))
                .order_by(Source.created_at.desc())
            )
            if status != "all":
                stmt = stmt.where(Source.status == status)
            if knowledge_type:
                kt_id = (await session.execute(
                    select(KnowledgeType.id).where(KnowledgeType.slug == knowledge_type)
                )).scalar()
                if kt_id:
                    stmt = stmt.where(Source.knowledge_type_id == kt_id)
            if query:
                stmt = stmt.where(
                    Source.title.ilike(f"%{query}%") |
                    Source.file_name.ilike(f"%{query}%") |
                    Source.url.ilike(f"%{query}%")
                )
            stmt = apply_scope_filter(stmt, identity).offset(offset).limit(limit)
            sources = (await session.execute(stmt)).scalars().all()

        if not sources:
            msg = "No documents found"
            if knowledge_type:
                msg += f" of type '{knowledge_type}'"
            if query:
                msg += f" matching '{query}'"
            return msg + "."

        from collections import defaultdict
        by_type = defaultdict(list)
        for s in sources:
            kt_name = s.knowledge_type.name if s.knowledge_type else "Uncategorized"
            by_type[kt_name].append(s)

        lines = [f"**Knowledge Base — {len(sources)} document(s)**\n"]
        for kt_name, type_sources in by_type.items():
            lines.append(f"\n### {kt_name} ({len(type_sources)})")
            for s in type_sources:
                title = s.title or s.file_name or s.url or "Untitled"
                lines.append(f"- **{title}** (ID: `{s.id}`)")
        return "\n".join(lines)

    @kb_tool(mcp, requires=ANY_AUTHENTICATED)
    @logged_tool("list_knowledge_types")
    async def list_knowledge_types() -> str:
        """
        List knowledge types (admin-defined classifications) accessible to the caller.
        """
        from sqlalchemy import func, select

        from app.database import async_session_factory
        from app.database.models import KnowledgeType, Source

        identity, err = await _get_identity()
        if err:
            return err
        assert identity is not None

        async with async_session_factory() as session:
            stmt = (
                select(KnowledgeType, func.count(Source.id).label("doc_count"))
                .outerjoin(
                    Source,
                    (Source.knowledge_type_id == KnowledgeType.id) & (Source.status == "ready"),
                )
                .group_by(KnowledgeType.id)
                .order_by(KnowledgeType.sort_order, KnowledgeType.name)
            )
            rows = (await session.execute(stmt)).all()

        if not rows:
            return "No knowledge types have been defined yet."

        allowed_types = identity.allowed_knowledge_types

        lines = ["**Knowledge Types**\n"]
        for kt, doc_count in rows:
            if allowed_types is not None and kt.slug not in allowed_types:
                continue
            line = f"- **{kt.name}** (slug: `{kt.slug}`, {doc_count} doc(s))"
            if kt.description:
                line += f" — {kt.description}"
            lines.append(line)

        if len(lines) == 1:
            return "No accessible knowledge types found for your scope."
        return "\n".join(lines)

    @kb_tool(mcp, requires=ANY_AUTHENTICATED)
    @logged_tool("get_knowledge_type_docs", query_arg="knowledge_type_slug")
    async def get_knowledge_type_docs(knowledge_type_slug: str, limit: int = 10) -> str:
        """
        List documents belonging to a specific knowledge type.

        Args:
            knowledge_type_slug: Type slug (use `list_knowledge_types` to find).
            limit: Max documents to return (default: 10).
        """
        from sqlalchemy import select

        from app.database import async_session_factory
        from app.database.models import KnowledgeType, Source
        from app.services.mcp_auth_service import apply_scope_filter

        identity, err = await _get_identity()
        if err:
            return err
        assert identity is not None

        if (identity.allowed_knowledge_types is not None
                and knowledge_type_slug not in identity.allowed_knowledge_types):
            return (
                f"Access denied: knowledge type '{knowledge_type_slug}' is outside your scope. "
                f"Use `list_knowledge_types` to see what types you can access."
            )

        async with async_session_factory() as session:
            kt = (await session.execute(
                select(KnowledgeType).where(KnowledgeType.slug == knowledge_type_slug)
            )).scalar_one_or_none()
            if not kt:
                return f"Knowledge type '{knowledge_type_slug}' not found."

            stmt = (
                select(Source)
                .where(Source.knowledge_type_id == kt.id, Source.status == "ready")
                .order_by(Source.created_at.desc())
            )
            stmt = apply_scope_filter(stmt, identity).limit(limit)
            sources = (await session.execute(stmt)).scalars().all()

        if not sources:
            return f"No documents found for knowledge type: **{kt.name}**"

        lines = [f"**{kt.name}** — {len(sources)} document(s)\n"]
        for s in sources:
            title = s.title or s.file_name or s.url or "Untitled"
            lines.append(f"- **{title}** (ID: `{s.id}`)")
        return "\n".join(lines)

    # =========================================================================
    # Tier 2 — Contribute (member-level, requires review)
    # =========================================================================

    @kb_tool(mcp, requires=CAN_CONTRIBUTE_WIKI)
    @logged_tool("propose_wiki_edit", query_arg="slug")
    async def propose_wiki_edit(
        slug: str,
        content_md: str,
        note: Optional[str] = None,
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
        base_version: Optional[int] = None,
    ) -> str:
        """
        Propose an edit to an existing wiki page. Creates a pending draft for editor review.

        Use search_wiki() or read_wiki_index() to find the right slug first.
        Always confirm with the user before submitting.

        Args:
            slug: Target page slug (e.g. "concept/fire-safety").
            content_md: The full proposed content in Markdown (max 50,000 chars).
            note: Optional one-line explanation of what you changed and why.
            scope_type: Optional — "global", "department", or "project". If the
                same slug exists in multiple scopes you MUST pass this to avoid
                ambiguity. Defaults to "global".
            scope_id: Required UUID when scope_type is "department" or "project".
            base_version: Version of the page this edit is based on. Captured
                automatically from read_wiki_page; passing the wrong value will
                cause the reviewer to see a conflict warning.
        """
        import uuid as _uuid

        from sqlalchemy import select

        from app.database import async_session_factory
        from app.database.models import Employee, WikiPage
        from app.services import wiki_service

        identity, err = await _get_identity()
        if err:
            return err
        assert identity is not None

        if not slug or not content_md.strip():
            return "Error: slug and content_md are required."
        if slug in ("_index", "_log"):
            return "Error: cannot propose drafts for reserved pages."
        if len(content_md) > 50_000:
            return "Error: content_md exceeds 50,000 character limit."

        sid: Optional[_uuid.UUID] = None
        if scope_id:
            try:
                sid = _uuid.UUID(scope_id)
            except ValueError:
                return "Error: scope_id must be a valid UUID."
        if scope_type and scope_type not in ("global", "department", "project"):
            return "Error: scope_type must be one of global, department, project."

        async with async_session_factory() as session:
            if scope_type:
                page = await wiki_service.get_page_by_slug(
                    session, slug, scope_type=scope_type, scope_id=sid,
                )
            else:
                # No explicit scope: require the slug to be unambiguous.
                matches = (await session.execute(
                    select(WikiPage).where(WikiPage.slug == slug)
                )).scalars().all()
                if len(matches) > 1:
                    scopes = ", ".join(
                        f"{m.scope_type}:{m.scope_id or 'global'}" for m in matches
                    )
                    return (
                        f"Error: slug '{slug}' exists in multiple scopes ({scopes}). "
                        "Re-call with scope_type and scope_id."
                    )
                page = matches[0] if matches else None
            if not page:
                return f"Page '{slug}' not found. Use read_wiki_index() to browse available pages."

            employee = await session.get(Employee, identity.employee_id)
            if not employee:
                return "Error: employee not found."

            if not await _can_contribute_to_page(session, employee, page):
                if page.scope_type == "project" and page.scope_id:
                    return f"Error: requires contributor role or above to propose edits to '{slug}'."
                return "Error: insufficient permission to propose wiki edits."

            effective_base = base_version if base_version is not None else page.version
            if (
                effective_base is not None
                and page.version is not None
                and effective_base > page.version
            ):
                return f"Error: base_version {effective_base} is ahead of current page v{page.version}."

            draft = await wiki_service.create_draft(
                session,
                page_id=page.id,
                author_id=employee.id,
                content_md=content_md.strip(),
                note=note,
                source="mcp_claude_desktop",
                base_version=effective_base,
            )
            draft.page = page
            from app.services import contribution_service
            from app.services.contribution_service import wiki_draft_adapter
            await contribution_service.notify_submitted(
                session, wiki_draft_adapter, draft, employee,
            )
            await session.commit()

        return (
            f"Draft submitted for `{slug}` (Draft ID: `{draft.id}`, based on v{effective_base}).\n"
            f"An editor will review it. Note: {note or '(none)'}"
        )

    # =========================================================================
    # Tier 3 — Direct Edit (editor/admin only, no review)
    # =========================================================================

    @kb_tool(mcp, requires=CAN_CREATE_WIKI_DIRECT)
    @logged_tool("edit_wiki_page", query_arg="slug")
    async def edit_wiki_page(
        slug: str,
        content_md: str,
        change_note: Optional[str] = None,
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
    ) -> str:
        """
        Directly edit a wiki page. Requires editor or admin role.
        Creates a revision in history immediately — no review step.

        Use propose_wiki_edit() instead if you only have contributor access.

        Args:
            slug: Target page slug.
            content_md: Full new content in Markdown.
            change_note: Optional one-line description of the change.
            scope_type: Optional — "global", "department", or "project". If the
                same slug exists in multiple scopes you MUST pass this. Defaults
                to "global" when only one match exists.
            scope_id: Required UUID when scope_type is "department" or "project".
        """
        import uuid as _uuid

        from sqlalchemy import select

        from app.database import async_session_factory
        from app.database.models import Employee, WikiPage
        from app.services import wiki_service

        identity, err = await _get_identity()
        if err:
            return err
        assert identity is not None

        if not slug or not content_md.strip():
            return "Error: slug and content_md are required."
        if slug in ("_index", "_log"):
            return "Error: cannot directly edit reserved pages."

        sid: Optional[_uuid.UUID] = None
        if scope_id:
            try:
                sid = _uuid.UUID(scope_id)
            except ValueError:
                return "Error: scope_id must be a valid UUID."
        if scope_type and scope_type not in ("global", "department", "project"):
            return "Error: scope_type must be one of global, department, project."

        async with async_session_factory() as session:
            if scope_type:
                page = await wiki_service.get_page_by_slug(
                    session, slug, scope_type=scope_type, scope_id=sid,
                )
            else:
                matches = (await session.execute(
                    select(WikiPage).where(WikiPage.slug == slug)
                )).scalars().all()
                if len(matches) > 1:
                    scopes = ", ".join(
                        f"{m.scope_type}:{m.scope_id or 'global'}" for m in matches
                    )
                    return (
                        f"Error: slug '{slug}' exists in multiple scopes ({scopes}). "
                        "Re-call with scope_type and scope_id."
                    )
                page = matches[0] if matches else None
            if not page:
                return f"Page '{slug}' not found."

            employee = await session.get(Employee, identity.employee_id)
            if not employee:
                return "Error: employee not found."

            if not await _can_review_page(session, employee, page):
                if page.scope_type == "project" and page.scope_id:
                    return f"Error: requires editor role or above to directly edit '{slug}'."
                return "Error: requires wiki:write:all permission to directly edit global wiki pages. Use propose_wiki_edit() instead."

            await wiki_service.direct_edit_page(session, page, employee.id, content_md.strip(), change_note)
            edited_scope_type = page.scope_type or "global"
            edited_scope_id = page.scope_id
            await wiki_service.regenerate_index(
                session, scope_type=edited_scope_type, scope_id=edited_scope_id,
            )
            await wiki_service.append_log(
                session,
                f"Edited page: {page.title} ({slug}) → v{page.version} via MCP by {employee.name or employee.email}",
                scope_type=edited_scope_type,
                scope_id=edited_scope_id,
            )
            await session.commit()
            await session.refresh(page)

        return f"Page `{slug}` updated to v{page.version}."

    # =========================================================================
    # Tier 4 — Review (editor/admin only)
    # =========================================================================

    @kb_tool(mcp, requires=CAN_REVIEW_WIKI)
    @logged_tool("list_pending_drafts")
    async def list_pending_drafts(
        workspace_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> str:
        """
        List pending wiki drafts awaiting editor review. Permission filtering
        is enforced at the SQL level so pagination is correct.

        Args:
            workspace_id: Optional. Filter to a specific workspace UUID.
                          Omit to see all accessible pending drafts.
            limit: Max drafts to return (default: 50).
            offset: Number of drafts to skip for pagination (default: 0).
        """
        from sqlalchemy import and_, select
        from sqlalchemy.orm import selectinload

        from app.database import async_session_factory
        from app.database.models import (
            Employee,
            ProjectMember,
            WikiPage,
            WikiPageDraft,
            WorkspaceRole,
        )
        from app.services.permission_engine import _get_user_permissions

        identity, err = await _get_identity()
        if err:
            return err
        assert identity is not None

        async with async_session_factory() as session:
            employee = await session.get(Employee, identity.employee_id)
            if not employee:
                return "Error: employee not found."

            perms = _get_user_permissions(employee)
            is_admin = employee.role == "admin"
            can_global = is_admin or "wiki:write:all" in perms

            stmt = (
                select(WikiPageDraft)
                .join(WikiPage, WikiPage.id == WikiPageDraft.page_id)
                .where(WikiPageDraft.status == "pending")
                .options(
                    selectinload(WikiPageDraft.page),
                    selectinload(WikiPageDraft.author),
                )
                .order_by(WikiPageDraft.created_at.asc())
                .offset(offset)
                .limit(limit)
            )

            if not can_global:
                editor_levels = [WorkspaceRole.EDITOR.value, WorkspaceRole.ADMIN.value]
                workspace_pages = select(ProjectMember.project_id).where(
                    ProjectMember.employee_id == employee.id,
                    ProjectMember.role.in_(editor_levels),
                )
                stmt = stmt.where(and_(
                    WikiPage.scope_type == "project",
                    WikiPage.scope_id.in_(workspace_pages),
                ))

            if workspace_id:
                stmt = stmt.where(WikiPage.scope_id == workspace_id)

            drafts = (await session.execute(stmt)).scalars().all()

            lines = []
            for draft in drafts:
                page = draft.page
                if not page:
                    continue
                author = draft.author
                lines.append(
                    f"- **{page.slug}** | Draft `{draft.id}` | "
                    f"by {author.name if author else 'unknown'} | "
                    f"{draft.created_at.strftime('%Y-%m-%d %H:%M')} | "
                    f"note: {draft.note or '(none)'}"
                )

        if not lines:
            return "No pending drafts found."
        return f"**{len(lines)} pending draft(s):**\n\n" + "\n".join(lines)

    @kb_tool(mcp, requires=CAN_REVIEW_WIKI)
    @logged_tool("review_draft", query_arg="draft_id")
    async def review_draft(draft_id: str) -> str:
        """
        Get full content of a pending draft for review.
        Returns the draft content alongside the current page content for comparison.

        Args:
            draft_id: UUID of the draft (from list_pending_drafts).
        """
        import uuid as _uuid

        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from app.database import async_session_factory
        from app.database.models import Employee, WikiPageDraft

        identity, err = await _get_identity()
        if err:
            return err
        assert identity is not None

        try:
            did = _uuid.UUID(draft_id)
        except ValueError:
            return "Error: invalid draft ID format."

        async with async_session_factory() as session:
            draft = (await session.execute(
                select(WikiPageDraft)
                .where(WikiPageDraft.id == did)
                .options(
                    selectinload(WikiPageDraft.page),
                    selectinload(WikiPageDraft.author),
                )
            )).scalar_one_or_none()
            if not draft:
                return f"Draft `{draft_id}` not found."

            employee = await session.get(Employee, identity.employee_id)
            if not employee:
                return "Error: employee not found."

            page = draft.page
            if not page:
                return "Error: parent wiki page not found."

            if not await _can_review_page(session, employee, page):
                return "Error: insufficient permission to review drafts for this page."

            author = draft.author

        return (
            f"## Draft `{draft_id}`\n"
            f"**Page:** `{page.slug}` — {page.title}\n"
            f"**Author:** {author.name if author else 'unknown'}\n"
            f"**Status:** {draft.status}\n"
            f"**Note:** {draft.note or '(none)'}\n\n"
            f"---\n\n"
            f"### Proposed content\n\n{draft.content_md}\n\n"
            f"---\n\n"
            f"### Current page content (v{page.version})\n\n{page.content_md or '_(empty)_'}"
        )

    @kb_tool(mcp, requires=CAN_REVIEW_WIKI)
    @logged_tool("approve_draft", query_arg="draft_id")
    async def approve_draft(
        draft_id: str,
        reviewer_note: Optional[str] = None,
        edited_content_md: Optional[str] = None,
        allow_conflict: bool = False,
    ) -> str:
        """
        Approve a pending wiki draft. Requires editor or admin role.

        The draft content (or your edited version) is written directly to the wiki page.
        A revision is created in history.

        Args:
            draft_id: UUID of the draft to approve.
            reviewer_note: Optional note to the author explaining the decision.
            edited_content_md: Optional — provide this to approve with your own edits
                               instead of the author's original content.
            allow_conflict: Set true to overwrite when the page has advanced past
                            the draft's base_version. Defaults to false (returns
                            a conflict error so the reviewer can re-base instead).
        """
        import uuid as _uuid

        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from app.database import async_session_factory
        from app.database.models import Employee, WikiPageDraft
        from app.services import wiki_service

        identity, err = await _get_identity()
        if err:
            return err
        assert identity is not None

        try:
            did = _uuid.UUID(draft_id)
        except ValueError:
            return "Error: invalid draft ID format."

        async with async_session_factory() as session:
            draft = (await session.execute(
                select(WikiPageDraft)
                .where(WikiPageDraft.id == did)
                .options(selectinload(WikiPageDraft.page))
            )).scalar_one_or_none()
            if not draft:
                return f"Draft `{draft_id}` not found."
            if draft.status != "pending":
                return f"Error: draft is already {draft.status}."

            employee = await session.get(Employee, identity.employee_id)
            if not employee:
                return "Error: employee not found."

            page = draft.page
            if not page:
                return "Error: parent wiki page not found."

            if not await _can_review_page(session, employee, page):
                return "Error: insufficient permission to approve drafts for this page."

            # Authors cannot approve their own drafts (admins exempt).
            if employee.role != "admin" and draft.author_id == employee.id:
                return "Error: you cannot approve your own draft. Ask another editor to review it."

            try:
                await wiki_service.approve_draft(
                    session, draft, employee.id,
                    reviewer_note=reviewer_note,
                    edited_content_md=edited_content_md,
                    allow_conflict=allow_conflict,
                )
            except wiki_service.DraftConflictError as e:
                return (
                    f"Conflict: {e}. Re-call with allow_conflict=true to overwrite "
                    "or supply edited_content_md after merging the latest changes."
                )
            approved_scope_type = page.scope_type or "global"
            approved_scope_id = page.scope_id
            await wiki_service.regenerate_index(
                session, scope_type=approved_scope_type, scope_id=approved_scope_id,
            )
            await wiki_service.append_log(
                session,
                f"Approved draft for: {page.title} ({page.slug}) → v{page.version} via MCP by {employee.name or employee.email}",
                scope_type=approved_scope_type,
                scope_id=approved_scope_id,
            )
            from app.services import contribution_service
            from app.services.contribution_service import wiki_draft_adapter
            await contribution_service.notify_approved(
                session, wiki_draft_adapter, draft, employee,
                version_label=f"v{page.version}",
            )
            await session.commit()

        return f"Draft `{draft_id}` approved. Page `{page.slug}` updated to v{page.version}."

    @kb_tool(mcp, requires=CAN_REVIEW_WIKI)
    @logged_tool("reject_draft", query_arg="draft_id")
    async def reject_draft(draft_id: str, reviewer_note: str) -> str:
        """
        Reject a pending wiki draft. reviewer_note is required — the author needs
        to understand why their proposal was not accepted.

        Args:
            draft_id: UUID of the draft to reject.
            reviewer_note: Required explanation for the author.
        """
        import uuid as _uuid

        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from app.database import async_session_factory
        from app.database.models import Employee, WikiPageDraft
        from app.services import wiki_service

        identity, err = await _get_identity()
        if err:
            return err
        assert identity is not None

        if not reviewer_note or not reviewer_note.strip():
            return "Error: reviewer_note is required when rejecting a draft."

        try:
            did = _uuid.UUID(draft_id)
        except ValueError:
            return "Error: invalid draft ID format."

        async with async_session_factory() as session:
            draft = (await session.execute(
                select(WikiPageDraft)
                .where(WikiPageDraft.id == did)
                .options(selectinload(WikiPageDraft.page))
            )).scalar_one_or_none()
            if not draft:
                return f"Draft `{draft_id}` not found."
            if draft.status != "pending":
                return f"Error: draft is already {draft.status}."

            employee = await session.get(Employee, identity.employee_id)
            if not employee:
                return "Error: employee not found."

            page = draft.page
            if not page:
                return "Error: parent wiki page not found."

            if not await _can_review_page(session, employee, page):
                return "Error: insufficient permission to reject drafts for this page."

            await wiki_service.reject_draft(session, draft, employee.id, reviewer_note.strip())
            from app.services import contribution_service
            from app.services.contribution_service import wiki_draft_adapter
            await contribution_service.notify_rejected(
                session, wiki_draft_adapter, draft, employee, reason=reviewer_note.strip(),
            )
            await session.commit()

        return f"Draft `{draft_id}` rejected. Note to author: {reviewer_note}"

    # =========================================================================
    # Tier 5 — needs_revision flow (request changes / resubmit / withdraw)
    # =========================================================================

    @kb_tool(mcp, requires=CAN_REVIEW_WIKI)
    @logged_tool("request_changes_on_draft", query_arg="draft_id")
    async def request_changes_on_draft(draft_id: str, reviewer_note: str) -> str:
        """
        Send a pending wiki draft back to the author for revisions.

        Use this instead of reject() when the contribution is on the right
        track but needs edits. The author can then resubmit via
        resubmit_draft() — the draft is kept and its revision_round bumps.

        Args:
            draft_id: UUID of the pending draft.
            reviewer_note: Required — explain what needs to change.
        """
        import uuid as _uuid

        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from app.database import async_session_factory
        from app.database.models import Employee, WikiPageDraft
        from app.services import contribution_service
        from app.services.contribution_service import (
            InvalidTransition,
            wiki_draft_adapter,
        )

        identity, err = await _get_identity()
        if err:
            return err
        assert identity is not None

        if not reviewer_note or not reviewer_note.strip():
            return "Error: reviewer_note is required when requesting changes."

        try:
            did = _uuid.UUID(draft_id)
        except ValueError:
            return "Error: invalid draft ID format."

        async with async_session_factory() as session:
            draft = (await session.execute(
                select(WikiPageDraft)
                .where(WikiPageDraft.id == did)
                .options(selectinload(WikiPageDraft.page))
            )).scalar_one_or_none()
            if not draft:
                return f"Draft `{draft_id}` not found."

            employee = await session.get(Employee, identity.employee_id)
            if not employee:
                return "Error: employee not found."

            page = draft.page
            if not page:
                return "Error: parent wiki page not found."
            if not await _can_review_page(session, employee, page):
                return "Error: insufficient permission to review drafts for this page."

            try:
                await contribution_service.request_changes(
                    session, wiki_draft_adapter, draft, employee, reviewer_note.strip(),
                )
            except InvalidTransition as e:
                return f"Error: {e}"
            await session.commit()

        return (
            f"Draft `{draft_id}` returned to author with note: {reviewer_note}\n"
            f"The author can resubmit when ready."
        )

    @kb_tool(mcp, requires=CAN_CONTRIBUTE_WIKI)
    @logged_tool("resubmit_draft", query_arg="draft_id")
    async def resubmit_draft(
        draft_id: str,
        content_md: str,
        note: Optional[str] = None,
    ) -> str:
        """
        Resubmit a draft that a reviewer sent back for changes (status:
        needs_revision). Author-only. Bumps revision_round and snapshots the
        prior submission to the rounds history.

        Args:
            draft_id: UUID of the draft to resubmit.
            content_md: New full content (max 50,000 chars).
            note: Optional one-line author note about this round.
        """
        import uuid as _uuid

        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from app.database import async_session_factory
        from app.database.models import Employee, WikiPageDraft
        from app.services import contribution_service
        from app.services.contribution_service import InvalidTransition

        identity, err = await _get_identity()
        if err:
            return err
        assert identity is not None

        if not content_md or not content_md.strip():
            return "Error: content_md is required."
        if len(content_md) > 50_000:
            return "Error: content_md exceeds 50,000 character limit."

        try:
            did = _uuid.UUID(draft_id)
        except ValueError:
            return "Error: invalid draft ID format."

        async with async_session_factory() as session:
            draft = (await session.execute(
                select(WikiPageDraft)
                .where(WikiPageDraft.id == did)
                .options(selectinload(WikiPageDraft.page))
            )).scalar_one_or_none()
            if not draft:
                return f"Draft `{draft_id}` not found."

            employee = await session.get(Employee, identity.employee_id)
            if not employee:
                return "Error: employee not found."

            try:
                await contribution_service.resubmit_wiki_draft(
                    session, draft, employee, content_md.strip(), author_note=note,
                )
            except InvalidTransition as e:
                return f"Error: {e}"
            await session.commit()

        return (
            f"Draft `{draft_id}` resubmitted (round {draft.revision_round}). "
            "Reviewers have been notified."
        )

    @kb_tool(mcp, requires=CAN_CONTRIBUTE_WIKI)
    @logged_tool("withdraw_draft", query_arg="draft_id")
    async def withdraw_draft(draft_id: str) -> str:
        """
        Withdraw your own draft (pending or needs_revision). Removes it from
        the reviewer queue. Author-only — admins can also withdraw via API.

        Args:
            draft_id: UUID of the draft to withdraw.
        """
        import uuid as _uuid

        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from app.database import async_session_factory
        from app.database.models import Employee, WikiPageDraft
        from app.services import contribution_service
        from app.services.contribution_service import (
            InvalidTransition,
            wiki_draft_adapter,
        )

        identity, err = await _get_identity()
        if err:
            return err
        assert identity is not None

        try:
            did = _uuid.UUID(draft_id)
        except ValueError:
            return "Error: invalid draft ID format."

        async with async_session_factory() as session:
            draft = (await session.execute(
                select(WikiPageDraft)
                .where(WikiPageDraft.id == did)
                .options(selectinload(WikiPageDraft.page))
            )).scalar_one_or_none()
            if not draft:
                return f"Draft `{draft_id}` not found."

            employee = await session.get(Employee, identity.employee_id)
            if not employee:
                return "Error: employee not found."

            try:
                await contribution_service.withdraw(
                    session, wiki_draft_adapter, draft, employee,
                )
            except InvalidTransition as e:
                return f"Error: {e}"
            await session.commit()

        return f"Draft `{draft_id}` withdrawn."

    # =========================================================================
    # Tier 6 — Create new pages (propose for contributors, direct for editors)
    # =========================================================================

    @kb_tool(mcp, requires=CAN_CONTRIBUTE_WIKI)
    @logged_tool("propose_wiki_create", query_arg="slug")
    async def propose_wiki_create(
        slug: str,
        title: str,
        content_md: str,
        page_type: str = "concept",
        knowledge_type_slugs: Optional[list[str]] = None,
        scope_type: str = "global",
        scope_id: Optional[str] = None,
        note: Optional[str] = None,
    ) -> str:
        """
        Propose a brand new wiki page for review. Contributor+ may file.
        The page is materialised when an editor approves the draft.

        Use search_wiki() first to check whether a similar page already
        exists — proposing duplicates wastes reviewer time.

        Args:
            slug: Unique URL slug, no whitespace, not _index or _log.
            title: Display title.
            content_md: Full Markdown content (max 50,000 chars).
            page_type: One of entity | concept | source | topic.
            knowledge_type_slugs: KB taxonomy tags (controls RBAC visibility).
            scope_type: "global" | "department" | "project".
            scope_id: Required UUID when scope_type is department or project.
            note: One-line description of why this page should exist.
        """
        import uuid as _uuid

        from app.database import async_session_factory
        from app.database.models import Employee
        from app.services import contribution_service, wiki_service
        from app.services.contribution_service import wiki_draft_adapter

        identity, err = await _get_identity()
        if err:
            return err
        assert identity is not None

        if not slug or not title or not content_md.strip():
            return "Error: slug, title, and content_md are required."
        slug = slug.strip()
        if slug in ("_index", "_log"):
            return "Error: '_index' and '_log' are reserved slugs."
        if len(content_md) > 50_000:
            return "Error: content_md exceeds 50,000 character limit."
        if any(c.isspace() for c in slug):
            return "Error: slug must not contain whitespace."
        if page_type not in wiki_service.PAGE_TYPES:
            return f"Error: page_type must be one of {sorted(wiki_service.PAGE_TYPES)}."
        if scope_type not in ("global", "department", "project"):
            return "Error: scope_type must be global, department, or project."

        sid: Optional[_uuid.UUID] = None
        if scope_id:
            try:
                sid = _uuid.UUID(scope_id)
            except ValueError:
                return "Error: scope_id must be a valid UUID."

        async with async_session_factory() as session:
            employee = await session.get(Employee, identity.employee_id)
            if not employee:
                return "Error: employee not found."

            # Permission gate matching REST propose_create_page.
            if employee.role != "admin":
                from app.services.permission_engine import (
                    _get_user_permissions,
                    get_workspace_role,
                    has_any_permission,
                    workspace_role_can,
                )
                perms = _get_user_permissions(employee)
                if scope_type == "project" and sid:
                    role = await get_workspace_role(session, employee, sid)
                    if not role or not workspace_role_can(role, "contributor"):
                        return "Error: requires contributor role or above in this workspace."
                elif scope_type == "department" and sid:
                    if "wiki:write:all" not in perms and not (
                        "wiki:write:own_dept" in perms and sid in employee.department_ids
                    ):
                        return "Error: insufficient permission to propose pages in this department."
                else:
                    if not has_any_permission(list(perms), "wiki", "write"):
                        return "Error: insufficient permission to propose new pages."

            existing = await wiki_service.get_page_by_slug(
                session, slug, scope_type=scope_type, scope_id=sid,
            )
            if existing is not None:
                return (
                    f"Error: page '{slug}' already exists in {scope_type}. "
                    "Use propose_wiki_edit() to suggest changes instead."
                )

            suggested_metadata = {
                "slug": slug, "title": title, "page_type": page_type,
                "knowledge_type_slugs": list(knowledge_type_slugs or []),
                "scope_type": scope_type,
                "scope_id": str(sid) if sid else None,
            }
            draft = await wiki_service.create_draft(
                session,
                page_id=None,
                author_id=employee.id,
                content_md=content_md.strip(),
                note=note,
                source="mcp_claude_desktop",
                base_version=None,
                draft_kind="create",
                suggested_metadata=suggested_metadata,
            )
            await contribution_service.notify_submitted(
                session, wiki_draft_adapter, draft, employee,
            )
            await session.commit()

        return (
            f"Create draft submitted for new page `{slug}` "
            f"(Draft ID: `{draft.id}`).\nAn editor will review and approve. "
            f"Note: {note or '(none)'}"
        )

    @kb_tool(mcp, requires=CAN_CREATE_WIKI_DIRECT)
    @logged_tool("create_wiki_page", query_arg="slug")
    async def create_wiki_page(
        slug: str,
        title: str,
        content_md: str,
        page_type: str = "concept",
        knowledge_type_slugs: Optional[list[str]] = None,
        scope_type: str = "global",
        scope_id: Optional[str] = None,
    ) -> str:
        """
        Directly create a new wiki page. Editor/admin only — no review step.

        Use propose_wiki_create() instead if you only have contributor access.

        Args:
            slug: Unique URL slug.
            title: Display title.
            content_md: Full Markdown content.
            page_type: entity | concept | source | topic.
            knowledge_type_slugs: KB taxonomy tags.
            scope_type: "global" | "department" | "project".
            scope_id: UUID when scope_type is department or project.
        """
        import uuid as _uuid

        from app.database import async_session_factory
        from app.database.models import Employee
        from app.services import wiki_service

        identity, err = await _get_identity()
        if err:
            return err
        assert identity is not None

        if not slug or not title or not content_md.strip():
            return "Error: slug, title, and content_md are required."
        slug = slug.strip()
        if slug in ("_index", "_log"):
            return "Error: '_index' and '_log' are reserved slugs."
        if any(c.isspace() for c in slug):
            return "Error: slug must not contain whitespace."
        if page_type not in wiki_service.PAGE_TYPES:
            return f"Error: page_type must be one of {sorted(wiki_service.PAGE_TYPES)}."
        if scope_type not in ("global", "department", "project"):
            return "Error: scope_type must be global, department, or project."

        sid: Optional[_uuid.UUID] = None
        if scope_id:
            try:
                sid = _uuid.UUID(scope_id)
            except ValueError:
                return "Error: scope_id must be a valid UUID."

        async with async_session_factory() as session:
            employee = await session.get(Employee, identity.employee_id)
            if not employee:
                return "Error: employee not found."

            # Permission: editor+ in workspace, wiki:write:all globally, or admin.
            if employee.role != "admin":
                from app.services.permission_engine import (
                    _get_user_permissions,
                    get_workspace_role,
                    workspace_role_can,
                )
                if scope_type == "project" and sid:
                    role = await get_workspace_role(session, employee, sid)
                    if not role or not workspace_role_can(role, "editor"):
                        return f"Error: requires editor role or above in this workspace."
                else:
                    perms = _get_user_permissions(employee)
                    if "wiki:write:all" not in perms:
                        return "Error: requires wiki:write:all permission. Use propose_wiki_create() instead."

            existing = await wiki_service.get_page_by_slug(
                session, slug, scope_type=scope_type, scope_id=sid,
            )
            if existing is not None:
                return f"Error: page '{slug}' already exists in {scope_type}."

            page = await wiki_service.apply_create(
                session,
                slug=slug, title=title, page_type=page_type,
                content_md=content_md.strip(), summary="",
                knowledge_type_slugs=list(knowledge_type_slugs or []),
                source_ids=[],
                scope_type=scope_type, scope_id=sid,
            )
            await wiki_service.regenerate_index(
                session, scope_type=scope_type, scope_id=sid,
            )
            await wiki_service.append_log(
                session,
                f"Created page: {title} ({slug}) via MCP by {employee.name or employee.email}",
                scope_type=scope_type, scope_id=sid,
            )
            await session.commit()

        return f"Page `{slug}` created at v{page.version}."
