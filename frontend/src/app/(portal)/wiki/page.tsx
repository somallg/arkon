"use client";

import React from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import { WikiPageSummary, WikiScope } from "@/types/wiki";
import { PageHeader } from "@/components/shared/page-header";
import { Button } from "@/components/ui/button";
import { WikiPageTree } from "@/components/wiki/wiki-page-tree";
import { WikiContent } from "@/components/wiki/wiki-content";
import { WikiTypeBadge, wikiTypeGroupLabel } from "@/components/wiki/wiki-type-badge";
import { ScopeBadge } from "@/components/shared/scope-badge";
import { WikiSearchDialog } from "@/components/wiki/wiki-search-dialog";
import { WikiScopeSwitcher } from "@/components/wiki/wiki-scope-switcher";
import { WikiCreatePageDialog } from "@/components/wiki/wiki-create-page-dialog";
import { EmptyState } from "@/components/shared/empty-state";
import { useAuth } from "@/lib/auth";

const WORKSPACE_ROLE_LEVEL: Record<string, number> = {
  viewer: 0,
  contributor: 1,
  editor: 2,
  admin: 3,
};
function roleAtLeast(role: string | null, min: string): boolean {
  if (!role) return false;
  return (WORKSPACE_ROLE_LEVEL[role] ?? -1) >= (WORKSPACE_ROLE_LEVEL[min] ?? 999);
}

const TYPE_TABS = ["all", "entity", "concept", "topic", "source"] as const;

export default function WikiIndexPage() {
  const searchParams = useSearchParams();
  const urlScopeType = searchParams.get("scope_type");
  const urlScopeId = searchParams.get("scope_id");

  const { user, getWorkspaceRole, hasPermission } = useAuth();

  const [indexMd, setIndexMd] = React.useState<string | null>(null);
  const [allPages, setAllPages] = React.useState<WikiPageSummary[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [searchOpen, setSearchOpen] = React.useState(false);
  const [createOpen, setCreateOpen] = React.useState(false);
  const [prefillTitle, setPrefillTitle] = React.useState("");
  const [activeTab, setActiveTab] = React.useState<string>("all");
  const [scopes, setScopes] = React.useState<WikiScope[]>([]);

  // `?new=1&title=<gap topic>` deep-link from the knowledge-gaps tab in
  // dashboard — auto-open the create dialog pre-filled with the
  // gap's normalized topic.
  React.useEffect(() => {
    if (searchParams.get("new") === "1") {
      setPrefillTitle(searchParams.get("title") || "");
      setCreateOpen(true);
    }
  }, [searchParams]);

  // Derive selected scope from URL params; fall back to the matching entry in
  // `scopes` once it loads so we can show the proper display name.
  const selectedScope: WikiScope = React.useMemo(() => {
    if (urlScopeType && urlScopeType !== "global") {
      const match = scopes.find(
        (s) => s.scope_type === urlScopeType && (s.scope_id ?? null) === (urlScopeId ?? null),
      );
      if (match) return match;
      return { scope_type: urlScopeType, scope_id: urlScopeId, name: urlScopeType };
    }
    return { scope_type: "global", scope_id: null, name: "Global" };
  }, [urlScopeType, urlScopeId, scopes]);

  // Fetch available scopes once so we can resolve the display name for the URL
  // scope. The switcher fetches its own copy — caching could optimise this but
  // /api/wiki/my-scopes is tiny.
  React.useEffect(() => {
    api<WikiScope[]>("/api/wiki/my-scopes")
      .then((s) => setScopes(Array.isArray(s) ? s : []))
      .catch(() => setScopes([]));
  }, []);

  // Refetch index + pages whenever the selected scope changes
  React.useEffect(() => {
    setLoading(true);
    const qs = selectedScope.scope_id
      ? `scope_type=${selectedScope.scope_type}&scope_id=${selectedScope.scope_id}`
      : `scope_type=${selectedScope.scope_type}`;
    Promise.all([
      api<{ content_md: string }>(`/api/wiki/index?${qs}`),
      api<WikiPageSummary[]>(`/api/wiki/pages?${qs}&limit=200`),
    ])
      .then(([idx, pages]) => {
        setIndexMd(idx.content_md || null);
        const filtered = Array.isArray(pages)
          ? pages.filter((p) => p.page_type !== "index" && p.page_type !== "log")
          : [];
        setAllPages(filtered);
      })
      .catch(() => {
        setIndexMd(null);
        setAllPages([]);
      })
      .finally(() => setLoading(false));
  }, [selectedScope.scope_type, selectedScope.scope_id]);

  React.useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setSearchOpen(true);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  // Permission helper: which create flow (if any) is available for the given
  // scope. Used to gate the header button and to surface per-scope `+`
  // buttons inside the page tree.
  const isAdmin = user?.role === "admin";
  const getCreateModeForScope = React.useCallback(
    (scope: { scope_type: string; scope_id: string | null }): "direct" | "propose" | null => {
      if (!user) return null;
      const st = scope.scope_type;
      const sid = scope.scope_id;
      if (st === "project" && sid) {
        const role = getWorkspaceRole(sid);
        if (isAdmin || roleAtLeast(role, "editor")) return "direct";
        if (roleAtLeast(role, "contributor")) return "propose";
        return null;
      }
      if (st === "department" && sid) {
        if (isAdmin || hasPermission("wiki:write:all")) return "direct";
        if (
          hasPermission("wiki:write:own_dept") &&
          user.department_ids.includes(sid)
        ) {
          return "propose";
        }
        return null;
      }
      // global
      if (isAdmin || hasPermission("wiki:write:all")) return "direct";
      if (hasPermission("wiki:write:own_dept")) return "propose";
      return null;
    },
    [user, isAdmin, getWorkspaceRole, hasPermission],
  );
  const createMode = getCreateModeForScope(selectedScope);

  // Scope the dialog opens against. The header button uses the currently
  // selected scope; the tree's `+` button overrides this on click.
  const [dialogScope, setDialogScope] = React.useState<WikiScope | null>(null);
  const dialogTargetScope: WikiScope = dialogScope ?? selectedScope;
  const dialogMode = getCreateModeForScope(dialogTargetScope);

  // Stats
  const totalPages = allPages.length;
  const typeCounts = React.useMemo(() => {
    const c: Record<string, number> = {};
    for (const p of allPages) c[p.page_type] = (c[p.page_type] ?? 0) + 1;
    return c;
  }, [allPages]);
  const lastUpdated = allPages[0]?.updated_at;

  // Filter by tab
  const displayPages = React.useMemo(() => {
    const list = activeTab === "all"
      ? allPages
      : allPages.filter((p) => p.page_type === activeTab);
    return list.slice(0, 24);
  }, [allPages, activeTab]);

  return (
    <>
      <PageHeader
        title="Knowledge Wiki"
        description="Compiled knowledge from your organization's documents."
        action={
          <div className="flex items-center gap-2">
            <WikiScopeSwitcher current={selectedScope} />
            <Button
              variant="outline"
              onClick={() => setSearchOpen(true)}
              className="gap-2"
            >
              <span className="material-symbols-outlined text-base">search</span>
              Search
              <kbd className="hidden sm:inline-block ml-1 px-1.5 py-0.5 rounded border border-border text-xs font-mono text-muted-foreground">
                ⌘K
              </kbd>
            </Button>
            {createMode && (
              <Button
                variant="outline"
                onClick={() => {
                  setDialogScope(null); // header button = current scope
                  setCreateOpen(true);
                }}
                className="gap-2"
                title={
                  createMode === "direct"
                    ? `Create a new page in ${selectedScope.name}`
                    : `Propose a new page in ${selectedScope.name} (reviewer approves)`
                }
              >
                <span className="material-symbols-outlined text-base">add</span>
                {createMode === "direct" ? "New page" : "Propose page"}
              </Button>
            )}
            {user && (
              <Link
                href="/wiki/review"
                className="inline-flex h-8 items-center gap-1.5 px-2.5 rounded-lg text-sm font-medium border border-border bg-background hover:bg-muted transition-colors"
                title="Drafts you authored and drafts waiting for your review"
              >
                <span className="material-symbols-outlined" style={{ fontSize: 16 }}>edit_note</span>
                Contributions
              </Link>
            )}
            <Link
              href="/wiki/graph"
              className="inline-flex h-8 items-center gap-1.5 px-2.5 rounded-lg text-sm font-medium bg-primary text-primary-foreground hover:bg-primary/90 transition-colors"
            >
              <span className="material-symbols-outlined" style={{ fontSize: 16 }}>hub</span>
              Graph View
            </Link>
          </div>
        }
      />

      <div className="flex-1 flex gap-0 -mx-6 md:-mx-8 lg:-mx-10 -mb-6 md:-mb-8 lg:-mb-10 min-h-0 border-t border-border">
        {/* Page Tree */}
        <WikiPageTree
          groupByScope
          activeScope={{
            scope_type: selectedScope.scope_type,
            scope_id: selectedScope.scope_id,
          }}
          getCreateModeForScope={(scope) =>
            getCreateModeForScope({
              scope_type: scope.scope_type,
              scope_id: scope.scope_id,
            })
          }
          onCreatePage={(scope) => {
            const match = scopes.find(
              (s) =>
                s.scope_type === scope.scope_type &&
                (s.scope_id ?? null) === (scope.scope_id ?? null),
            );
            setDialogScope(
              match ?? {
                scope_type: scope.scope_type,
                scope_id: scope.scope_id,
                name: scope.scope_type,
              },
            );
            setCreateOpen(true);
          }}
        />

        {/* Content */}
        <div className="flex-1 overflow-y-auto px-8 py-6">
          {loading ? (
            <div className="flex items-center justify-center h-32">
              <span className="material-symbols-outlined text-3xl text-muted-foreground animate-spin">
                progress_activity
              </span>
            </div>
          ) : indexMd ? (
            <>
              {/* Stats bar */}
              {totalPages > 0 && (
                <div className="flex flex-wrap items-center gap-3 mb-8">
                  <div className="flex items-center gap-2 bg-card border border-border rounded-xl px-4 py-2.5 shadow-sahara">
                    <span className="material-symbols-outlined text-base text-primary">article</span>
                    <span className="text-sm font-semibold text-foreground">{totalPages}</span>
                    <span className="text-xs text-muted-foreground">Pages</span>
                  </div>
                  {Object.entries(typeCounts).sort((a, b) => b[1] - a[1]).map(([type, count]) => (
                    <div
                      key={type}
                      className="flex items-center gap-1.5 bg-card border border-border rounded-xl px-3 py-2.5 shadow-sahara"
                    >
                      <WikiTypeBadge type={type} />
                      <span className="text-xs text-muted-foreground tabular-nums">{count}</span>
                    </div>
                  ))}
                  {lastUpdated && (
                    <div className="flex items-center gap-2 bg-card border border-border rounded-xl px-4 py-2.5 shadow-sahara ml-auto">
                      <span className="material-symbols-outlined text-base text-muted-foreground">schedule</span>
                      <span className="text-xs text-muted-foreground">
                        Updated {new Date(lastUpdated).toLocaleDateString("en-US", {
                          month: "short",
                          day: "numeric",
                        })}
                      </span>
                    </div>
                  )}
                </div>
              )}

              <WikiContent markdown={indexMd} />

              {/* Pages grid */}
              {allPages.length > 0 && (
                <div className="mt-12">
                  {/* Type tabs */}
                  <div className="flex items-center gap-1 mb-5 border-b border-border">
                    {TYPE_TABS.map((tab) => {
                      const count = tab === "all"
                        ? totalPages
                        : typeCounts[tab] ?? 0;
                      if (tab !== "all" && count === 0) return null;
                      return (
                        <button
                          key={tab}
                          onClick={() => setActiveTab(tab)}
                          className={`px-3 py-2 text-xs font-medium capitalize border-b-2 transition-colors ${
                            activeTab === tab
                              ? "border-primary text-primary"
                              : "border-transparent text-muted-foreground hover:text-foreground"
                          }`}
                        >
                          {tab === "all" ? "All" : wikiTypeGroupLabel(tab)}
                          <span className="ml-1.5 tabular-nums text-muted-foreground">
                            {count}
                          </span>
                        </button>
                      );
                    })}
                  </div>

                  <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
                    {displayPages.map((page) => {
                      const cardHref =
                        page.scope_type && page.scope_type !== "global" && page.scope_id
                          ? `/wiki/${page.slug}?scopeType=${page.scope_type}&scopeId=${page.scope_id}`
                          : `/wiki/${page.slug}`;
                      return (
                      <Link
                        key={`${page.slug}-${page.scope_type ?? "global"}-${page.scope_id ?? "none"}`}
                        href={cardHref}
                        className="group block bg-card border border-border rounded-xl p-4 hover:border-primary/40 hover:shadow-sahara transition-all"
                      >
                        <div className="flex items-start justify-between gap-2 mb-2">
                          <div className="flex items-center gap-1.5 flex-wrap">
                            <WikiTypeBadge type={page.page_type} />
                            {page.scope_type && page.scope_type !== "global" && (
                              <ScopeBadge scopeType={page.scope_type} scopeId={page.scope_id} />
                            )}
                          </div>
                          <span className="text-xs text-muted-foreground shrink-0">
                            v{page.version}
                          </span>
                        </div>
                        <h3 className="font-heading text-base font-normal text-foreground group-hover:text-primary transition-colors mb-1">
                          {page.title}
                        </h3>
                        {page.summary && (
                          <p className="text-xs text-muted-foreground line-clamp-2">
                            {page.summary}
                          </p>
                        )}
                        <p className="text-xs text-muted-foreground mt-3">
                          {new Date(page.updated_at).toLocaleDateString()}
                        </p>
                      </Link>
                      );
                    })}
                  </div>
                </div>
              )}
            </>
          ) : (
            <EmptyState
              icon="auto_stories"
              title="Wiki is empty"
              description="Upload and compile documents to start building your knowledge wiki."
            />
          )}
        </div>
      </div>

      <WikiSearchDialog open={searchOpen} onOpenChange={setSearchOpen} />
      {dialogMode && (
        <WikiCreatePageDialog
          open={createOpen}
          onOpenChange={(o) => {
            setCreateOpen(o);
            if (!o) {
              setDialogScope(null);
              setPrefillTitle("");
            }
          }}
          mode={dialogMode}
          defaultScope={dialogTargetScope}
          scopes={scopes}
          getCreateModeForScope={(s) =>
            getCreateModeForScope({
              scope_type: s.scope_type,
              scope_id: s.scope_id ?? null,
            })
          }
          defaultTitle={prefillTitle}
        />
      )}
    </>
  );
}
