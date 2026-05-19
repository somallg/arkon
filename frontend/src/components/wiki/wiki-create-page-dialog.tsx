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
import { Textarea } from "@/components/ui/textarea";
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
  /** Scopes the user is allowed to write to. */
  scopes: WikiScope[];
};

const PAGE_TYPES = ["entity", "concept", "topic", "source"] as const;

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
}: Props) {
  const router = useRouter();
  const [title, setTitle] = React.useState("");
  const [slug, setSlug] = React.useState("");
  const [slugTouched, setSlugTouched] = React.useState(false);
  const [pageType, setPageType] = React.useState<(typeof PAGE_TYPES)[number]>("concept");
  const [scopeKey, setScopeKey] = React.useState(
    `${defaultScope.scope_type}:${defaultScope.scope_id ?? ""}`,
  );
  const [content, setContent] = React.useState("");
  const [note, setNote] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  // Auto-derive slug from title until the user edits it manually.
  React.useEffect(() => {
    if (!slugTouched) setSlug(slugify(title));
  }, [title, slugTouched]);

  // Reset on open.
  React.useEffect(() => {
    if (!open) return;
    setTitle("");
    setSlug("");
    setSlugTouched(false);
    setPageType("concept");
    setScopeKey(`${defaultScope.scope_type}:${defaultScope.scope_id ?? ""}`);
    setContent("");
    setNote("");
    setError(null);
  }, [open, defaultScope]);

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
      if (mode === "direct") {
        const page = await api<WikiPageDetail>("/api/wiki/pages", {
          method: "POST",
          body: {
            slug,
            title,
            page_type: pageType,
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
            page_type: pageType,
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
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>
            {mode === "direct" ? "Create new page" : "Propose new page"}
          </DialogTitle>
          <DialogDescription>
            {mode === "direct"
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

          <div className="grid grid-cols-2 gap-3">
            <div className="grid gap-1.5">
              <Label htmlFor="cp-type">Page type</Label>
              <select
                id="cp-type"
                value={pageType}
                onChange={(e) => setPageType(e.target.value as (typeof PAGE_TYPES)[number])}
                className="h-9 rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
              >
                {PAGE_TYPES.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </div>

            <div className="grid gap-1.5">
              <Label htmlFor="cp-scope">Scope</Label>
              <select
                id="cp-scope"
                value={scopeKey}
                onChange={(e) => setScopeKey(e.target.value)}
                className="h-9 rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
              >
                {scopes.map((s) => (
                  <option
                    key={`${s.scope_type}:${s.scope_id ?? ""}`}
                    value={`${s.scope_type}:${s.scope_id ?? ""}`}
                  >
                    {s.name} {s.scope_type !== "global" ? `(${s.scope_type})` : ""}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="grid gap-1.5">
            <Label htmlFor="cp-content">Content (Markdown)</Label>
            <Textarea
              id="cp-content"
              value={content}
              onChange={(e) => setContent(e.target.value)}
              placeholder="# Overview&#10;&#10;Use [[wikilinks]] to reference other pages."
              className="min-h-64 font-mono text-sm"
            />
          </div>

          {mode === "propose" && (
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
                {mode === "direct" ? "add" : "send"}
              </span>
            )}
            {mode === "direct" ? "Create page" : "Submit proposal"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
