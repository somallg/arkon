"use client";

import React from "react";
import { useRouter } from "next/navigation";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { MarkdownEditor } from "./markdown-editor";
import { api } from "@/lib/api";
import { WikiScope, WikiPageDetail, DraftResponse } from "@/types/wiki";

type Mode = "direct" | "propose";

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** "direct" = POST /wiki/pages (editor+); "propose" = POST /wiki/drafts/create */
  mode: Mode;
  /** Pre-selected scope. User can still change it within the dialog. */
  defaultScope: WikiScope;
  /** All scopes the user can READ. The dropdown filters to writable ones. */
  scopes: WikiScope[];
  /** Returns the create flow available in `scope` (or null when none). When
   *  provided, the scope dropdown drops options where this returns null so
   *  the user cannot accidentally pick a scope they have no write permission
   *  for, and the dialog title / submit endpoint follow the picked scope. */
  getCreateModeForScope?: (scope: { scope_type: string; scope_id: string | null }) => Mode | null;
  /** Optional initial title — used when opening from a knowledge-gap suggestion. */
  defaultTitle?: string;
};

function slugify(s: string): string {
  return s
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/đ/g, "d")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

export function WikiCreatePageDialog({
  open,
  onOpenChange,
  mode,
  defaultScope,
  scopes,
  getCreateModeForScope,
  defaultTitle = "",
}: Props) {
  const router = useRouter();
  const [title, setTitle] = React.useState(defaultTitle);
  const [slug, setSlug] = React.useState("");
  const [slugTouched, setSlugTouched] = React.useState(false);
  const [scopeKey, setScopeKey] = React.useState(
    `${defaultScope.scope_type}:${defaultScope.scope_id ?? ""}`,
  );
  const [content, setContent] = React.useState("");
  const [note, setNote] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  // Restrict the scope dropdown to scopes the user can actually write to.
  // Without this filter a user with wiki:write:own_dept (and wiki:read:all)
  // sees every department in `scopes` and can pick someone else's dept,
  // leaking a cross-department draft past the contextual permission gate.
  const writableScopes = React.useMemo(() => {
    if (!getCreateModeForScope) return scopes;
    return scopes.filter((s) =>
      getCreateModeForScope({
        scope_type: s.scope_type,
        scope_id: s.scope_id ?? null,
      }) !== null,
    );
  }, [scopes, getCreateModeForScope]);

  // Effective mode follows the currently-selected scope so 'direct' switches
  // to 'propose' when the user picks a scope where they only have own_dept.
  const [pickedScopeType, pickedScopeIdRaw] = scopeKey.split(":");
  const pickedScopeId = pickedScopeIdRaw || null;
  const effectiveMode: Mode = getCreateModeForScope
    ? getCreateModeForScope({ scope_type: pickedScopeType, scope_id: pickedScopeId }) ?? mode
    : mode;

  // Auto-derive slug from title until the user edits it manually.
  React.useEffect(() => {
    if (!slugTouched) setSlug(slugify(title));
  }, [title, slugTouched]);

  // Reset on open.
  React.useEffect(() => {
    if (!open) return;
    setTitle(defaultTitle);
    setSlug("");
    setSlugTouched(false);
    setScopeKey(`${defaultScope.scope_type}:${defaultScope.scope_id ?? ""}`);
    setContent("");
    setNote("");
    setError(null);
  }, [open, defaultScope, defaultTitle]);

  const submit = async () => {
    if (!title.trim() || !slug.trim() || !content.trim()) {
      setError("Title, slug, and content are required.");
      return;
    }
    const [scope_type, scope_id_raw] = scopeKey.split(":");
    const scope_id = scope_id_raw || null;

    setBusy(true);
    setError(null);
    try {
      if (effectiveMode === "direct") {
        const page = await api<WikiPageDetail>("/api/wiki/pages", {
          method: "POST",
          body: {
            slug,
            title,
            page_type: "concept",
            content_md: content,
            scope_type,
            scope_id,
            knowledge_type_slugs: [],
          },
        });
        onOpenChange(false);
        const qs =
          scope_type === "global"
            ? ""
            : `?scopeType=${scope_type}&scopeId=${scope_id}`;
        router.push(`/wiki/${page.slug}${qs}`);
      } else {
        const draft = await api<DraftResponse>("/api/wiki/drafts/create", {
          method: "POST",
          body: {
            slug,
            title,
            page_type: "concept",
            content_md: content,
            scope_type,
            scope_id,
            knowledge_type_slugs: [],
            note: note || null,
          },
        });
        onOpenChange(false);
        // Drafts land in the reviewer queue — no page exists yet. Just close.
        // The user will get a notification when it is approved.
        // eslint-disable-next-line no-console
        console.info("Draft submitted:", draft.id);
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Submit failed";
      setError(msg);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-3xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>
            {effectiveMode === "direct" ? "Create new page" : "Propose new page"}
          </DialogTitle>
          <DialogDescription>
            {effectiveMode === "direct"
              ? "The page is created immediately and added to the index."
              : "An editor will review the proposal before the page is materialised."}
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-4 py-2">
          <div className="grid gap-1.5">
            <Label htmlFor="cp-title">Title</Label>
            <Input
              id="cp-title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="e.g. Fire Safety Procedure"
              autoFocus
            />
          </div>

          <div className="grid gap-1.5">
            <Label htmlFor="cp-slug">
              Slug <span className="text-muted-foreground font-normal">(URL identifier)</span>
            </Label>
            <Input
              id="cp-slug"
              value={slug}
              onChange={(e) => {
                setSlugTouched(true);
                setSlug(e.target.value);
              }}
              placeholder="fire-safety-procedure"
              className="font-mono text-sm"
            />
          </div>

          <div className="grid gap-1.5">
            <Label htmlFor="cp-scope">Scope</Label>
            <select
              id="cp-scope"
              value={scopeKey}
              onChange={(e) => setScopeKey(e.target.value)}
              className="h-9 rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
            >
              {writableScopes.map((s) => (
                <option
                  key={`${s.scope_type}:${s.scope_id ?? ""}`}
                  value={`${s.scope_type}:${s.scope_id ?? ""}`}
                >
                  {s.name} {s.scope_type !== "global" ? `(${s.scope_type})` : ""}
                </option>
              ))}
            </select>
          </div>

          <div className="grid gap-1.5">
            <Label>Content</Label>
            <MarkdownEditor
              value={content}
              onChange={setContent}
              placeholder={"# Overview\n\nUse [[wikilinks]] to reference other pages."}
              minHeightClass="min-h-[280px]"
            />
          </div>

          {effectiveMode === "propose" && (
            <div className="grid gap-1.5">
              <Label htmlFor="cp-note">
                Note for reviewer <span className="text-muted-foreground font-normal">(optional)</span>
              </Label>
              <Input
                id="cp-note"
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="One line: why does this page need to exist?"
              />
            </div>
          )}

          {error && <p className="text-sm text-destructive">{error}</p>}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={busy} className="gap-1.5">
            {busy ? (
              <span className="material-symbols-outlined text-sm animate-spin">
                progress_activity
              </span>
            ) : (
              <span className="material-symbols-outlined text-sm">
                {effectiveMode === "direct" ? "add" : "send"}
              </span>
            )}
            {effectiveMode === "direct" ? "Create page" : "Submit proposal"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
