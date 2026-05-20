# Wiki System

The Arkon wiki is the primary knowledge surface. Instead of storing raw document chunks, Arkon compiles documents into structured, interlinked wiki pages — written by an LLM agent, enriched by every new document you add.

---

## How compilation works

When you upload a document, the background worker runs the **MRP pipeline** — a five-phase deterministic process that guarantees every section of the document is read and every claim is traceable back to its source.

```
Phase 0: Triage   → classify document size/strategy
Phase 1: MAP      → chunk document + parallel LLM extraction per chunk
Phase 2: REDUCE   → entity dedup + KB reconciliation → Compilation Plan
Phase 2.5: Review → human approves / modifies / rejects the plan
Phase 3: REFINE   → parallel page writers (one per planned page)
Phase 4: VERIFY   → citation check + coverage check + conflict check
Phase 5: COMMIT   → write pages to DB + embed + regenerate index
```

### Phase 0 — Triage

Classifies the document to choose the right processing strategy based on length:

| Strategy | Document size | Description |
|---|---|---|
| `single_pass` | < 30K chars | Entire document fits in one extraction call |
| `standard` | 30K–200K chars | Split into ~20K-char chunks along section headings |
| `hierarchical` | > 200K chars | Same as standard but with additional context stitching |

### Phase 1 — MAP

The document is split into chunks aligned to section headings (from `outline_json`). Each chunk is ~20K characters with a 1K-char overlap prefix from the previous section. Chunks are processed in parallel (up to 6 at once).

Each chunk extraction call returns structured JSON:

```json
{
  "entities": [{ "name": "...", "type": "person|org|product|...", "local_offset": 0 }],
  "concepts": [{ "term": "...", "definition_excerpt": "...", "local_offset": 0 }],
  "claims":   [{ "statement": "...", "subject": "...", "local_offset": 0, "evidence_length": 200 }],
  "relations": [{ "from": "...", "to": "...", "type": "..." }],
  "topics":   ["..."]
}
```

`local_offset` values are converted to `absolute_offset` (byte position in the original document) so every claim can be traced back to its exact source excerpt. Each chunk's result is saved to `source_chunk_extracts` immediately — if the worker crashes, MAP resumes from where it left off.

### Phase 2 — REDUCE

All chunk extracts are merged into a unified knowledge graph:

1. **Exact dedup** — normalize entity names (lowercase + strip punctuation), group duplicates
2. **Embedding dedup** — cosine similarity between entity name vectors; auto-merge above 0.90, LLM disambiguates 0.75–0.90
3. **KB reconciliation** — semantic search against existing wiki pages per entity:
   - sim ≥ 0.85 → `UPDATE` candidate (entity has an existing page)
   - sim 0.60–0.85 → LLM confirms whether to merge or create new
   - sim < 0.60 → `CREATE` candidate
4. **Planning call** — a single LLM call produces the **Compilation Plan**: a prioritized list of pages to create or update, each with entity coverage and cross-link targets

The plan is saved to `source_compilation_plans` with `status = pending_review`.

### Phase 2.5 — Human plan review

Before any pages are written, an editor reviews the Compilation Plan:

- **Portal:** Knowledge Base → source row with "Review Plan" status → click to open the plan review dialog
- **API:** `GET /api/sources/{id}/plan` → `POST /api/sources/{id}/plan/approve` or `/reject`

The plan shows every page that will be created or updated, its type, and the entities it will cover. Editors can approve as-is, submit a modified plan (reorder, rename, remove pages), or reject with a note.

**Auto-approve** is available for CI/CD or trusted pipelines: set `MRP_AUTO_APPROVE_PLAN=true` in your environment.

### Phase 3 — REFINE

Each planned page gets its own writer. Writers run in parallel (up to 4 at once). Every writer receives pre-assembled evidence — the relevant claims with their source excerpts — so it never needs to scan the full document.

> **Concurrency note.** Each REFINE writer opens its **own** `AsyncSession`. Sharing a single session across concurrent writers caused `IllegalStateChangeError` in 0.7.0 — fixed in commit `484e8d1`. If you ever add a new layer that touches the DB during REFINE, make sure it uses the per-writer session, not a shared one.

**Simple writer** (≤ 8 evidence items, existing page ≤ 3K chars): a single `llm.generate()` call.

**Complex writer** (larger pages): a mini agent loop (max 10 steps) with tools:

| Tool | Purpose |
|---|---|
| `read_kb_page` | Read any existing wiki page for cross-referencing |
| `read_source_excerpt` | Read more context from the source document |
| `finish` | Submit the completed page content |

Every factual claim in the written content is marked with a `[^N]` footnote citation.

### Phase 4 — VERIFY

Three non-blocking checks run after REFINE:

1. **Citation verification** — each `[^N]` claim is checked against its source excerpt by the LLM. Verdicts: `SUPPORTED` (no change), `PARTIAL` (caveat added), `NOT_SUPPORTED` (marked `[unverified]`), `CONTRADICTED` (flagged with warning marker).

2. **Coverage check** — entities mentioned ≥ 3 times in chunk extracts but not covered by any planned page are logged as warnings.

3. **Conflict check** — new page content is embedded and compared against existing KB pages. Semantically similar pages (sim > 0.80) are checked for factual contradictions by the LLM and logged.

All three checks are informational — they never block the pipeline.

### Phase 5 — COMMIT

All verified pages are written to the database in a single atomic transaction. For each page:
- `CREATE` → `wiki_service.apply_create()`
- `UPDATE` → `wiki_service.apply_update()` (falls back to create if the page was deleted)

After all pages are flushed, the wiki index is regenerated, an activity log entry is appended, and the source is marked `ready`.

### Resume behavior

The field `source.pipeline_phase` tracks which phase completed last. If the worker crashes, the next retry picks up from the right phase:

| `pipeline_phase` | Behavior on retry |
|---|---|
| `map` | Skip chunks already extracted, process remaining |
| `reduce` | Re-run REDUCE (all chunks already done) |
| `plan_review` | Return existing plan — do not re-run MAP+REDUCE |
| `refine` / `verify` / `commit` | Re-run REFINE from plan (in-memory results are regenerated) |

### Page types

| Type | Description |
|---|---|
| `entity` | A named thing: person, company, product, location |
| `concept` | A process, rule, methodology, or framework |
| `topic` | A broad subject area |
| `source` | A page representing the source document itself |

---

## Wiki page structure

Each page is stored with:
- `slug` — URL-safe identifier (e.g. `concept/fire-safety`, `entity/acme-corp`)
- `title` — human-readable name
- `page_type` — entity / concept / topic / source
- `content_md` — full markdown content
- `summary` — one-sentence summary for index and search
- `knowledge_type_slugs[]` — which knowledge types this page belongs to
- `source_ids[]` — which source documents contributed to this page
- `embedding` — vector for semantic search (pgvector)
- `scope_type` + `scope_id` — global or project-scoped
- `version` — current version number
- `orphaned` — true if all contributing sources have been deleted

---

## Version history

Every change to a wiki page creates an immutable revision record:

```
WikiPageRevision
  page_id       → which page
  version       → monotonically increasing integer
  content_md    → full snapshot of the content at this version
  change_type   → agent_compile | editor_edit | draft_approved | rollback
  changed_by_id → which employee (null for agent compilations)
  change_note   → optional description
  draft_id      → linked draft if change_type = draft_approved
```

### Accessing revision history

- **Portal:** Wiki page → History tab → list of all versions
- **API:** `GET /api/wiki/pages/{slug}/revisions`

### Rollback

Admins can restore any previous version:
- **Portal:** History tab → select version → Rollback
- **API:** `POST /api/wiki/pages/{slug}/revisions/{version}/rollback`

Rollback creates a new revision with `change_type=rollback` — the history is preserved, not overwritten.

---

## Editing wiki pages

Two paths depending on your role:

### Direct edit (Editor / Admin)

Editors can edit a page directly — no review step. The change takes effect immediately and a revision is created.

- **Portal:** Open wiki page → Edit button
- **API:** `PUT /api/wiki/pages/{slug}`
- **MCP:** `edit_wiki_page(slug, content_md, change_note)`

Requires: **workspace editor+** for workspace-scoped pages, or **`wiki:write:all`** for global pages.

### Propose a draft (Contributor)

Contributors propose edits that go through editor review before being applied.

- **Portal:** Open wiki page → Propose Edit
- **API:** `POST /api/wiki/pages/{slug}/drafts`
- **MCP:** `propose_wiki_edit(slug, content_md, note)`

Requires: **workspace contributor+** for workspace-scoped pages, or **`wiki:write:own_dept`** for global pages.

---

## Draft workflow

The full state machine, including the `needs_revision` loop:

```
                ┌──────────────────────────────────────────┐
                │                                          │
                ▼                                          │
   Contributor submits  ──────────►  pending               │
                                       │                   │
                       Editor approve  │                   │
                                       ▼                   │
                                   approved (terminal)     │
                                                           │
                       Editor reject (note required)       │
                                       ▼                   │
                                   rejected (terminal)     │
                                                           │
                  Editor request_changes (note required)   │
                                       ▼                   │
                              needs_revision ──────────────┘
                                       │       (author resubmits content_md
                                       │        → bumps revision_round
                                       │        → snapshots previous round
                                       │           to wiki_draft_rounds)
                                       │
                       Author withdraw │
                                       ▼
                                   withdrawn (terminal)
```

Multiple drafts can be pending for the same page at the same time. Editors resolve them one by one — approving a draft applies its content. Sibling drafts on the same page are checked for version conflict (`base_version < page.version`); the reviewer can re-base, supply edited content, or pass `allow_conflict=true` to overwrite.

### Concurrency guards on approve

`wiki_service.approve_draft` is wrapped in a Postgres advisory lock keyed on `hashtext(slug)`:

- Two reviewers approving different drafts on the **same page** at the same time would otherwise both read `page.version=N`, both set `N+1`, and both insert a `WikiPageRevision(version=N+1)` — a duplicate row and a last-writer-wins overwrite on `content_md`. The lock serialises them, and the page row is `refresh()`'d **inside** the critical section so the second approver sees the bumped version.
- Bulk approve wraps **each** draft in `db.begin_nested()` (a SAVEPOINT) so a conflict / IntegrityError on one row no longer poisons the outer transaction. The router also `db.expire()`s the touched draft and page on failure, because SAVEPOINT rollback reverts the database but **not** the ORM identity map — a later iteration would otherwise read the polluted `page.version` from RAM.

### Cross-author notifications

When a draft is approved, every other author with a still-pending draft on the same page is notified ("page advanced while your draft was pending — re-base or withdraw"). Notifications are deduped per author within the approve event and batched into one INSERT via `notification_service.notify_each`.

### Draft rounds (history)

Every `request_changes` → `resubmit` cycle snapshots the previous content (and AI verdict) into `wiki_draft_rounds(draft_id, round_no, content_md, author_note, reviewer_return_note, ai_check_results, submitted_at)`. Visible via `GET /api/wiki/drafts/{id}/rounds`.

---

## AI pre-review (L1 → L4)

When a draft is submitted (or resubmitted), the arq worker runs an automated pre-review and writes the verdict to `wiki_page_drafts.ai_check_results` (jsonb). The submit path only **enqueues** the job — it never runs synchronously, so a slow LLM call cannot block draft creation.

| Layer | What it checks | Cost |
|---|---|---|
| **L1 regex** | Email / phone / API key shaped patterns | Free |
| **L2 structural** | Markdown well-formedness, broken wikilinks, headings sanity | Free |
| **L3 semantic** | Duplicate / near-duplicate of other wiki pages (cosine sim on embedding). Skipped for `draft_kind="edit"` since the page is supposed to overlap with itself. | Embedding call |
| **L4 LLM** | Tone + factuality sanity (lightweight prompt) | 1 LLM call |

`ai_check_status` transitions: `pending` → `queued` (after enqueue) → `running` (worker started) → one of `passed | warned | failed | skipped`.

Permissive by design: no layer ever blocks submission — even a `failed` L4 verdict only annotates the draft so the reviewer sees the flag. If Redis is down at submit time, status falls back to `skipped` so the UI never shows a stuck `queued`.

### Resubmit race guard

The enqueue call passes the draft's `revision_round` at that moment. Before the worker writes its final verdict, it `refresh()`'s the draft and bails if `revision_round` has bumped — the user has resubmitted, the in-memory content is stale, and a newer worker is already queued for the new content. Without this guard a slow L4 run could overwrite a fresh verdict with an out-of-date one. Code: [app/services/ai_review/runner.py](../app/services/ai_review/runner.py).

### Stuck-running sweep

A cron (`sweep_stuck_ai_review_cron`, runs every 10 minutes) flips any draft stuck in `ai_check_status='running'` for longer than `2 × WORKER_JOB_TIMEOUT` (min 30 min) back to `skipped`. This catches the SIGKILL / OOM / container-restart cases that the worker's own try/except can't, so the UI never shows a perpetual spinner.

---

## Review console — `/wiki/review`

For high-volume reviewers, the inline page banner is replaced by a dedicated 3-pane console at `/wiki/review`. URL state (`?draft=&status=&mine=`) is the source of truth — deep links, browser back/forward, and "share this draft" all work.

| Pane | Width | Content |
|---|---|---|
| Left | 320px | Queue list with scope toggle (To review / Mine), status filter, auto-snap to first item on filter change |
| Center | flex | Sticky header (title, scope chip, link to full page), tab toggle (diff / proposed / current), compare-with dropdown across sibling drafts on the same page |
| Right | 340px | Author + stats, submission metadata, suggested-metadata (for create drafts), suggested reviewers, AI pre-review panel, action stack (Approve / Request changes / Reject) — or Withdraw when the draft is the viewer's own |

Keyboard shortcuts (disabled inside form fields):

| Key | Action |
|---|---|
| `j` / `↓` | Next draft |
| `k` / `↑` | Previous draft |
| `a` | Approve |
| `c` | Request changes (opens note + focuses textarea) |
| `r` | Reject (opens note + focuses textarea) |
| `Esc` | Cancel pending action |
| `?` | Toggle shortcut help overlay |

After any terminal action the draft is removed from the local list and the next one auto-selects — no page reload. AI status auto-polls every 3 s while running.

The existing `WikiDraftBanner` on `/wiki/[slug]` is intentionally kept for casual reviewers who land on a page; the console is the power-user surface.

### Editor review actions

**Via portal:** Wiki Drafts queue → select draft → compare side-by-side → Approve or Reject.

**Via API:**
- `GET /api/wiki/drafts` — list pending drafts (filtered to your scope)
- `GET /api/wiki/pages/{slug}/drafts` — drafts for a specific page
- `GET /api/wiki/drafts/{id}` — full draft with current page content
- `POST /api/wiki/drafts/{id}/approve` — approve (optionally with edited content)
- `POST /api/wiki/drafts/{id}/reject` — reject (reviewer_note required)

**Via MCP (for Claude Desktop editors):**
- `list_pending_drafts(workspace_id?)` — see pending drafts
- `review_draft(draft_id)` — read draft vs current content
- `approve_draft(draft_id, reviewer_note?, edited_content_md?, allow_conflict?)`
- `reject_draft(draft_id, reviewer_note)`
- `request_changes_on_draft(draft_id, reviewer_note)` — send back without rejecting
- `resubmit_draft(draft_id, content_md, note?)` — author resubmits after changes
- `withdraw_draft(draft_id)` — author withdraws their own draft

**Bulk approve** for queue cleanup:
- `POST /api/wiki/drafts/bulk-approve` `{draft_ids: [...], allow_conflict?, reviewer_note?}` — per-draft savepoint, returns `{added, skipped, errored, results[]}`.

**Create-kind drafts** (propose a brand-new page rather than editing one):
- `POST /api/wiki/drafts/create` — contributor-level, becomes a `WikiPageDraft(draft_kind="create", page_id=NULL)` with `suggested_metadata` (slug, title, page_type, scope). The reviewer can override metadata before materialising the page on approve.
- MCP: `propose_wiki_create(slug, title, content_md, ...)`
- Direct create (editor+, no review): `create_wiki_page(slug, title, content_md, ...)`

---

## Scope: Global vs. Workspace

Wiki pages are either global or workspace-scoped:

**Global pages** — visible to all employees who have `wiki:read` permission.
Compiled from global sources (documents not assigned to any specific workspace).

**Workspace-scoped pages** — visible only to workspace members.
Compiled from workspace-owned sources. Accessible through the workspace wiki browser.

When a source is uploaded directly into a workspace (via the workspace Sources tab), its compiled wiki pages are automatically scoped to that workspace.

---

## Orphaned pages

When all source documents contributing to a wiki page are deleted, the page is marked `orphaned = true`. It is NOT automatically deleted — editors can review orphaned pages and decide whether to keep, update, or remove them.

- **API:** `GET /api/wiki/orphaned` (admin only)

---

## Knowledge graph

Wiki pages are linked via `[[wikilinks]]` in their content. Arkon extracts these links into a `wiki_links` table, enabling:

- **Backlinks** — which pages link to this one
- **Outlinks** — which pages this one links to
- **Graph visualization** — interactive node/edge graph in the portal

The full graph is available at `/wiki/graph`. Each workspace also has a scoped graph at `GET /api/projects/{id}/wiki/graph`.

---

## Wiki index and log

Two reserved pages are maintained automatically:

- `_index` — a catalog of all wiki pages, updated after each compilation
- `_log` — a chronological log of ingestion and compilation events

These are visible in the wiki browser and accessible via:
- `GET /api/wiki/index`
- `GET /api/wiki/log`
