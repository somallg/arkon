"use client";

import React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { WikiPageSummary } from "@/types/wiki";
import { wikiTypeIcon, wikiTypeColor, wikiTypeGroupLabel } from "./wiki-type-badge";

const GROUP_ORDER = ["entity", "concept", "topic", "source"];

// Scope type ordering for grouped view: global → department → project.
const SCOPE_TYPE_ORDER: Record<string, number> = {
  global: 0,
  department: 1,
  project: 2,
};

const SCOPE_ICONS: Record<string, string> = {
  global: "public",
  department: "corporate_fare",
  project: "folder_special",
};

function scopeGroupKey(p: WikiPageSummary): string {
  const st = p.scope_type || "global";
  return p.scope_id ? `${st}:${p.scope_id}` : st;
}

function scopeGroupLabel(p: WikiPageSummary): string {
  const st = p.scope_type || "global";
  if (st === "global") return "Global";
  return p.scope_name || (st === "department" ? "Department" : "Workspace");
}

function pageKey(p: WikiPageSummary): string {
  return `${p.slug}-${p.scope_type || "global"}-${p.scope_id ?? "none"}`;
}

function useDebounce<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = React.useState(value);
  React.useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return debounced;
}

export function WikiPageTree({
  activeSlug,
  onDeleted,
  pagesUrl,
  linkQueryParams,
  onPageSelect,
  groupByScope = false,
  activeScope,
  getCreateModeForScope,
  onCreatePage,
}: {
  activeSlug?: string;
  onDeleted?: () => void;
  /** Override the API URL to load pages from (default: /api/wiki/pages) */
  pagesUrl?: string;
  /** Query params to append to page links (e.g. "?scopeType=project&scopeId=xxx") */
  linkQueryParams?: string;
  /** If provided, clicks call this instead of navigating via Link */
  onPageSelect?: (slug: string) => void;
  /** When true, render a 2-level tree: scope → page_type → pages (used in /wiki). */
  groupByScope?: boolean;
  /** Auto-expand the bucket matching this scope (used together with groupByScope). */
  activeScope?: { scope_type: string; scope_id: string | null };
  /** Optional: return which create flow ("direct" | "propose" | null) applies for a scope.
   *  When provided AND non-null, the tree shows a `+` button on that scope's header. */
  getCreateModeForScope?: (scope: { scope_type: string; scope_id: string | null }) => "direct" | "propose" | null;
  /** Called when the user clicks the per-scope `+` button. */
  onCreatePage?: (scope: { scope_type: string; scope_id: string | null }) => void;
}) {
  const pathname = usePathname();
  const [pages, setPages] = React.useState<WikiPageSummary[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [search, setSearch] = React.useState("");
  const [collapsed, setCollapsed] = React.useState(false);
  const [expandedGroups, setExpandedGroups] = React.useState<Set<string>>(
    new Set(GROUP_ORDER)
  );
  // Two-stage delete
  const [armedSlug, setArmedSlug] = React.useState<string | null>(null);
  const [deletingSlug, setDeletingSlug] = React.useState<string | null>(null);

  // Sources section
  const [sources, setSources] = React.useState<{ id: string; title: string; file_name?: string; status: string; source_type?: string }[]>([]);
  const [sourcesLoading, setSourcesLoading] = React.useState(true);
  const [sourcesCollapsed, setSourcesCollapsed] = React.useState(false);

  const debouncedSearch = useDebounce(search, 150);

  const loadPages = React.useCallback(() => {
    const url = pagesUrl || "/api/wiki/pages?limit=200";
    api<WikiPageSummary[]>(url)
      .then((data) => setPages(Array.isArray(data) ? data : []))
      .catch(() => setPages([]))
      .finally(() => setLoading(false));
  }, [pagesUrl]);

  React.useEffect(() => {
    loadPages();
  }, [loadPages]);

  React.useEffect(() => {
    api<{ items: { id: string; title: string; file_name?: string; status: string; source_type?: string }[] }>("/api/sources?status=ready&page_size=200")
      .then((data) => setSources(data.items || []))
      .catch(() => setSources([]))
      .finally(() => setSourcesLoading(false));
  }, []);

  const handleDelete = async (page: WikiPageSummary) => {
    const slug = page.slug;
    // First click: arm; second click: execute
    if (armedSlug !== slug) {
      setArmedSlug(slug);
      return;
    }
    setArmedSlug(null);
    setDeletingSlug(slug);
    try {
      // Pass scope so the backend deletes the right copy when the same slug
      // exists in multiple scopes (e.g. global + project).
      const scopeQs =
        page.scope_type && page.scope_type !== "global" && page.scope_id
          ? `?scope_type=${page.scope_type}&scope_id=${page.scope_id}`
          : "";
      await api(`/api/wiki/pages/${encodeURIComponent(slug)}${scopeQs}`, {
        method: "DELETE",
      });
      loadPages();
      onDeleted?.();
    } catch (err) {
      console.error("Delete failed:", err);
    } finally {
      setDeletingSlug(null);
    }
  };

  // Click outside armed row → disarm
  React.useEffect(() => {
    if (!armedSlug) return;
    const handler = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      if (!target.closest(`[data-slug="${armedSlug}"]`)) {
        setArmedSlug(null);
      }
    };
    document.addEventListener("click", handler, true);
    return () => document.removeEventListener("click", handler, true);
  }, [armedSlug]);

  const filtered = React.useMemo(() => {
    if (!debouncedSearch) return pages;
    const q = debouncedSearch.toLowerCase();
    return pages.filter(
      (p) =>
        p.title.toLowerCase().includes(q) ||
        p.slug.toLowerCase().includes(q) ||
        p.summary.toLowerCase().includes(q)
    );
  }, [pages, debouncedSearch]);

  const grouped = React.useMemo(() => {
    return [...filtered]
      .filter((p) => p.page_type !== "index" && p.page_type !== "log" && p.page_type !== "hot")
      .sort((a, b) => a.title.localeCompare(b.title));
  }, [filtered]);

  // Scope grouping (used when groupByScope=true).
  // Outer map keyed by scopeGroupKey(); value is { label, scope_type, scope_id, pages, total }.
  const scopeGrouped = React.useMemo(() => {
    type ScopeBucket = {
      key: string;
      label: string;
      scope_type: string;
      scope_id: string | null;
      pages: WikiPageSummary[];
      total: number;
    };
    const map = new Map<string, ScopeBucket>();
    for (const p of filtered) {
      if (p.page_type === "index" || p.page_type === "log" || p.page_type === "hot") continue;
      // Workspaces (project scope) are reached via /workspaces — keep the
      // /wiki tree focused on enterprise-wide knowledge (global + departments).
      if ((p.scope_type || "global") === "project") continue;
      const k = scopeGroupKey(p);
      let bucket = map.get(k);
      if (!bucket) {
        bucket = {
          key: k,
          label: scopeGroupLabel(p),
          scope_type: p.scope_type || "global",
          scope_id: p.scope_id ?? null,
          pages: [],
          total: 0,
        };
        map.set(k, bucket);
      }
      bucket.pages.push(p);
      bucket.total += 1;
    }
    
    // Sort pages alphabetically in each bucket
    for (const b of map.values()) {
      b.pages.sort((a, b) => a.title.localeCompare(b.title));
    }
    
    // Sort buckets: scope type order first, then label alphabetical.
    return Array.from(map.values()).sort((a, b) => {
      const ao = SCOPE_TYPE_ORDER[a.scope_type] ?? 99;
      const bo = SCOPE_TYPE_ORDER[b.scope_type] ?? 99;
      if (ao !== bo) return ao - bo;
      return a.label.localeCompare(b.label);
    });
  }, [filtered]);

  const totalCount = filtered.filter(
    (p) => p.page_type !== "index" && p.page_type !== "log" && p.page_type !== "hot"
  ).length;

  // Expanded state for scope-level headers in groupByScope mode.
  // Default: expand global; collapse the rest to keep the tree compact.
  const [expandedScopes, setExpandedScopes] = React.useState<Set<string>>(
    new Set(["global"]),
  );
  const activeScopeKey = React.useMemo(() => {
    if (!activeScope) return null;
    return activeScope.scope_id
      ? `${activeScope.scope_type}:${activeScope.scope_id}`
      : activeScope.scope_type;
  }, [activeScope]);
  React.useEffect(() => {
    if (!groupByScope || scopeGrouped.length === 0) return;

    const scopesToExpand = new Set<string>(["global"]);
    if (activeScopeKey) scopesToExpand.add(activeScopeKey);
    if (activeSlug) {
      for (const b of scopeGrouped) {
        if (b.pages.some((p) => p.slug === activeSlug)) {
          scopesToExpand.add(b.key);
        }
      }
    }

    setExpandedScopes((prev) => {
      const next = new Set(prev);
      for (const k of scopesToExpand) next.add(k);
      return next;
    });
  }, [groupByScope, scopeGrouped, activeSlug, activeScopeKey]);

  const toggleScope = (key: string) =>
    setExpandedScopes((prev) => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });

  const currentSlug = activeSlug ?? pathname.replace(/^\/wiki\//, "");

  // Renders one leaf row (page link + delete button).
  const renderPageItem = (page: WikiPageSummary) => {
    const isActive = page.slug === currentSlug;
    const isArmed = armedSlug === page.slug;
    const isDeleting = deletingSlug === page.slug;
    const linkSuffix = linkQueryParams
      ? linkQueryParams
      : page.scope_type && page.scope_type !== "global" && page.scope_id
        ? `?scopeType=${page.scope_type}&scopeId=${page.scope_id}`
        : "";

    const statusColors: Record<string, string> = {
      seed: "bg-[#a8977e]/20 border-[#a8977e]/50",
      developing: "bg-[#d4872e]/20 border-[#d4872e]/50",
      mature: "bg-[#2e8b8b]/20 border-[#2e8b8b]/50",
      evergreen: "bg-[#3a8a3f]/20 border-[#3a8a3f]/50",
    };
    const statusText: Record<string, string> = {
      seed: "Seed",
      developing: "Developing",
      mature: "Mature",
      evergreen: "Evergreen",
    };

    return (
      <div
        key={pageKey(page)}
        data-slug={page.slug}
        className={cn(
          "group flex items-center gap-1 rounded-lg mx-1 transition-all",
          isActive ? "bg-primary/10" : "hover:bg-accent/50",
        )}
      >
        {onPageSelect ? (
          <button
            onClick={() => onPageSelect(page.slug)}
            className={cn(
              "flex-1 flex items-center gap-2 px-2.5 py-1.5 text-xs min-w-0 transition-all text-left",
              isActive ? "text-primary font-medium" : "text-muted-foreground hover:text-foreground",
            )}
            title={page.summary || page.title}
          >
            <span
              className={cn("w-1.5 h-1.5 rounded-full shrink-0 border", statusColors[page.status || "seed"])}
              title={`Status: ${statusText[page.status || "seed"]}`}
            />
            <span className="truncate">{page.title}</span>
          </button>
        ) : (
          <Link
            href={`/wiki/${page.slug}${linkSuffix}`}
            className={cn(
              "flex-1 flex items-center gap-2 px-2.5 py-1.5 text-xs min-w-0 transition-all",
              isActive ? "text-primary font-medium" : "text-muted-foreground hover:text-foreground",
            )}
            title={page.summary || page.title}
          >
            <span
              className={cn("w-1.5 h-1.5 rounded-full shrink-0 border", statusColors[page.status || "seed"])}
              title={`Status: ${statusText[page.status || "seed"]}`}
            />
            <span className="truncate">{page.title}</span>
          </Link>
        )}

        {isDeleting ? (
          <span className="material-symbols-outlined text-xs text-destructive animate-pulse mr-1.5">
            progress_activity
          </span>
        ) : isArmed ? (
          <button
            onClick={(e) => {
              e.stopPropagation();
              handleDelete(page);
            }}
            className="shrink-0 mr-1 px-1.5 py-0.5 rounded text-[10px] font-semibold bg-destructive text-destructive-foreground hover:bg-destructive/90 animate-pulse transition-colors"
            title={`Click again to confirm delete "${page.title}"`}
          >
            Confirm
          </button>
        ) : (
          <button
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              handleDelete(page);
            }}
            className="shrink-0 mr-1 opacity-0 group-hover:opacity-100 text-muted-foreground hover:text-destructive transition-all"
            title={`Delete "${page.title}"`}
          >
            <span className="material-symbols-outlined" style={{ fontSize: 14 }}>delete</span>
          </button>
        )}
      </div>
    );
  };

  if (collapsed) {
    return (
      <div className="w-10 border-r border-border bg-card/30 flex flex-col items-center pt-4 gap-3 shrink-0">
        <button
          onClick={() => setCollapsed(false)}
          className="text-muted-foreground hover:text-foreground transition-colors"
          title="Expand page tree"
        >
          <span className="material-symbols-outlined text-base">left_panel_open</span>
        </button>
      </div>
    );
  }

  return (
    <div className="w-64 shrink-0 border-r border-border bg-card/30 flex flex-col overflow-hidden">
      {/* === PAGES SECTION === */}
      <div className="flex flex-col min-h-0" style={{ flex: sourcesCollapsed ? '1 1 auto' : '1 1 55%' }}>
      {/* Header */}
      <div className="flex items-center gap-2 px-4 py-3 border-b border-border">
        <span className="text-xs font-semibold text-muted-foreground uppercase tracking-wider flex-1">
          Pages
        </span>
        <span className="text-xs text-muted-foreground tabular-nums bg-muted rounded-md px-1.5 py-0.5">
          {totalCount}
        </span>
        <button
          onClick={() => setCollapsed(true)}
          className="text-muted-foreground hover:text-foreground transition-colors"
          title="Collapse"
        >
          <span className="material-symbols-outlined text-base">left_panel_close</span>
        </button>
      </div>

      {/* Search */}
      <div className="px-3 py-2 border-b border-border">
        <div className="flex items-center gap-2 bg-background border border-border rounded-lg px-2.5 py-1.5">
          <span className="material-symbols-outlined text-sm text-muted-foreground">
            search
          </span>
          <input
            type="text"
            placeholder="Filter pages..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="flex-1 text-xs bg-transparent outline-none text-foreground placeholder:text-muted-foreground"
          />
          {search && (
            <button
              onClick={() => setSearch("")}
              className="text-muted-foreground hover:text-foreground"
            >
              <span className="material-symbols-outlined text-sm">close</span>
            </button>
          )}
        </div>
      </div>

      {/* Tree */}
      <div className="flex-1 overflow-y-auto py-2">
        {loading ? (
          <div className="px-3 space-y-2 mt-1">
            {Array.from({ length: 6 }).map((_, i) => (
              <div
                key={i}
                className="h-7 rounded-md bg-muted animate-pulse"
                style={{ opacity: 1 - i * 0.12 }}
              />
            ))}
          </div>
        ) : groupByScope ? (
          scopeGrouped.length === 0 ? (
            <p className="text-xs text-muted-foreground px-4 py-3">No pages found.</p>
          ) : (
            scopeGrouped.map((bucket) => {
              const scopeExpanded = expandedScopes.has(bucket.key);
              const isActive = activeScopeKey === bucket.key;
              const scopeHref =
                bucket.scope_type === "global"
                  ? "/wiki"
                  : bucket.scope_id
                    ? `/wiki?scope_type=${bucket.scope_type}&scope_id=${bucket.scope_id}`
                    : `/wiki?scope_type=${bucket.scope_type}`;
              return (
                <div key={bucket.key} className="mb-2">
                  <div
                    className={cn(
                      "flex items-center gap-1 px-1 transition-colors rounded-md",
                      isActive ? "bg-accent/50" : "hover:bg-accent/30",
                    )}
                  >
                    <button
                      onClick={() => toggleScope(bucket.key)}
                      className="shrink-0 p-1 text-muted-foreground hover:text-foreground transition-colors"
                      title={scopeExpanded ? "Collapse" : "Expand"}
                    >
                      <span className="material-symbols-outlined text-xs">
                        {scopeExpanded ? "expand_more" : "chevron_right"}
                      </span>
                    </button>
                    <Link
                      href={scopeHref}
                      className="flex-1 flex items-center gap-2 py-1.5 min-w-0 text-left"
                      title={`Open ${bucket.label} wiki`}
                    >
                      <span
                        className="material-symbols-outlined text-xs text-muted-foreground"
                        style={{ fontSize: 13 }}
                      >
                        {SCOPE_ICONS[bucket.scope_type] ?? "tune"}
                      </span>
                      <span
                        className={cn(
                          "text-xs font-semibold uppercase tracking-wide flex-1 truncate",
                          isActive ? "text-primary" : "text-foreground",
                        )}
                      >
                        {bucket.label}
                      </span>
                      <span className="text-xs text-muted-foreground tabular-nums">
                        {bucket.total}
                      </span>
                    </Link>
                    {(() => {
                      if (!onCreatePage || !getCreateModeForScope) return null;
                      const mode = getCreateModeForScope({
                        scope_type: bucket.scope_type,
                        scope_id: bucket.scope_id ?? null,
                      });
                      if (!mode) return null;
                      return (
                        <button
                          type="button"
                          onClick={(e) => {
                            e.preventDefault();
                            e.stopPropagation();
                            onCreatePage({
                              scope_type: bucket.scope_type,
                              scope_id: bucket.scope_id ?? null,
                            });
                          }}
                          className="shrink-0 mr-1 w-5 h-5 flex items-center justify-center rounded text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
                          title={
                            mode === "direct"
                              ? `New page in ${bucket.label}`
                              : `Propose new page in ${bucket.label}`
                          }
                          aria-label={`Create page in ${bucket.label}`}
                        >
                          <span className="material-symbols-outlined" style={{ fontSize: 14 }}>
                            add
                          </span>
                        </button>
                      );
                    })()}
                  </div>
                  {scopeExpanded && (
                    <div className="ml-5 mt-1 space-y-0.5 border-l border-border/30 pl-2">
                      {bucket.pages.length === 0 ? (
                        <p className="text-[10px] text-muted-foreground px-3 py-1 italic">Empty</p>
                      ) : (
                        bucket.pages.map((page) => renderPageItem(page))
                      )}
                    </div>
                  )}
                </div>
              );
            })
          )
        ) : grouped.length === 0 ? (
          <p className="text-xs text-muted-foreground px-4 py-3">No pages found.</p>
        ) : (
          <div className="space-y-0.5">
            {grouped.map((page) => renderPageItem(page))}
          </div>
        )}
      </div>
      </div>

      {/* === SOURCES SECTION === */}
      <div className="flex flex-col min-h-0 border-t border-border" style={{ flex: sourcesCollapsed ? '0 0 auto' : '1 1 45%' }}>
        {/* Sources header */}
        <div className="flex items-center gap-2 px-4 py-2.5 border-b border-border">
          <button
            onClick={() => setSourcesCollapsed(!sourcesCollapsed)}
            className="text-muted-foreground hover:text-foreground transition-colors"
          >
            <span className="material-symbols-outlined text-xs">
              {sourcesCollapsed ? "chevron_right" : "expand_more"}
            </span>
          </button>
          <span className="text-xs font-semibold text-muted-foreground uppercase tracking-wider flex-1">
            Sources
          </span>
          <span className="text-xs text-muted-foreground tabular-nums bg-muted rounded-md px-1.5 py-0.5">
            {sources.length}
          </span>
        </div>

        {/* Sources list */}
        {!sourcesCollapsed && (
          <div className="flex-1 overflow-y-auto py-1">
            {sourcesLoading ? (
              <div className="px-3 space-y-2 mt-1">
                {Array.from({ length: 3 }).map((_, i) => (
                  <div key={i} className="h-6 rounded-md bg-muted animate-pulse" style={{ opacity: 1 - i * 0.2 }} />
                ))}
              </div>
            ) : sources.length === 0 ? (
              <p className="text-[10px] text-muted-foreground px-4 py-2 italic">No sources uploaded.</p>
            ) : (
              <div className="space-y-0.5">
                {sources.map((src) => (
                  <Link
                    key={src.id}
                    href={`/wiki/source/${src.id}`}
                    className={cn(
                      "group flex items-center gap-2 px-3 py-1.5 mx-1 text-xs rounded-lg transition-all",
                      currentSlug === `source/${src.id}`
                        ? "bg-primary/10 text-primary font-medium"
                        : "text-muted-foreground hover:text-foreground hover:bg-accent/50",
                    )}
                    title={src.title || src.file_name}
                  >
                    <span className="material-symbols-outlined shrink-0" style={{ fontSize: 14 }}>
                      {src.source_type === "url" ? "link" : "description"}
                    </span>
                    <span className="truncate">{src.title || src.file_name || "Untitled"}</span>
                  </Link>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
