# Architecture

## System overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        On-Premise Server                         │
│                                                                  │
│  ┌──────────────────┐        ┌───────────────────────────────┐  │
│  │   Admin Portal   │        │        Arkon API              │  │
│  │   (Next.js)      │──────▶ │        (FastAPI)              │  │
│  └──────────────────┘        │                               │  │
│                               │  /api/*   REST endpoints      │  │
│                               │  /mcp     MCP server          │  │
│                               │  /docs    Swagger UI          │  │
│                               └───────────────┬───────────────┘  │
│                                               │                  │
│  ┌────────────────┐  ┌──────────────┐        │                  │
│  │ Wiki Worker    │  │ Skill Worker │        │                  │
│  │ (arq)          │  │ (arq)        │        │                  │
│  │ · ingestion    │  │ · skill pkg  │        │                  │
│  │ · compilation  │  │   processing │        │                  │
│  └────────────────┘  └──────────────┘        │                  │
│                                               │                  │
│  ┌──────────────┐  ┌───────────┐  ┌────────┐│                  │
│  │  PostgreSQL  │  │   Redis   │  │ MinIO  ││                  │
│  │  + pgvector  │  │  (queue)  │  │(files) ││                  │
│  └──────────────┘  └───────────┘  └────────┘│                  │
└───────────────────────────────────────────────┼─────────────────┘
                                                │ MCP (HTTPS)
                               ┌────────────────┼────────────┐
                               │                │            │
                          Claude Desktop    Claude.ai    Any MCP
                          (employees)       (web)        client
```

---

## Components

### Admin Portal (`frontend/`)
Next.js application. Provides the UI for:
- Knowledge base and document management
- Wiki browser (three-panel: page tree, content, backlinks/outlinks)
- Workspace management and member roles
- RBAC configuration (departments, roles, permissions)
- Employee accounts and MCP token management
- AI Skills library
- Audit log

### Arkon API (`app/`)
FastAPI application serving two protocols simultaneously:

**REST API** (`/api/*`) — used by the Admin Portal and direct integrations:
- Auth: JWT-based session for portal users
- Sources: document upload, URL ingestion, recompilation
- Wiki: page CRUD, drafts, revisions, graph
- Projects (Workspaces): member management, scoped sources and wiki
- RBAC: departments, employees, roles, permissions
- Skills: upload, version management, scope assignment
- Audit: activity log

**MCP Server** (`/mcp`) — used by Claude Desktop and other MCP clients:
- Auth: bearer token (MCP token per employee)
- Tools: wiki search/read, source drill-down, skill access
- All responses are filtered by the employee's permission scope

### Background Workers (`app/worker.py`)
Two arq (Redis-based) worker pools:

**Wiki Worker** (`WorkerSettings`):
- `ingest_file_task` — extract text and images from uploaded files, then enqueue `ingest_map_reduce_task`
- `ingest_url_task` — scrape and extract text from URLs, then enqueue `ingest_map_reduce_task`
- `ingest_map_reduce_task` — MRP Phase 0-2: triage, parallel chunk extraction (MAP), entity dedup + KB reconciliation + compilation plan (REDUCE)
- `ingest_refine_task` — MRP Phase 3-5: parallel page writers (REFINE), citation/coverage/conflict checks (VERIFY), atomic DB write (COMMIT). Each writer opens its own `AsyncSession`.
- `caption_images_task` — vision-model captioning for embedded images, enqueues `ingest_map_reduce_task` when done
- `regenerate_plan_task` — re-runs Phase 2 (REDUCE + planning call) on demand
- `reembed_all_pages_task` — backfill / re-embedding when the active embedding spec changes
- `ai_pre_review_draft_task` — runs all four AI-review layers (L1 regex → L2 structural → L3 semantic → L4 LLM) on a wiki draft. Payload includes `expected_round` so a stale worker drops its verdict when the author has resubmitted mid-flight.

**Cron jobs** (declared in `WorkerSettings.cron_jobs`):
- `daily_stats_rollup_cron` (02:00 UTC) — recomputes admin Statistics rollups for the previous UTC day. Idempotent — re-running overwrites prior rows via the unique constraint on `(date, metric_key, dimensions_hash)`.
- `sweep_stuck_ai_review_cron` (every 10 min) — flips any draft stuck in `ai_check_status='running'` for longer than `2 × worker_job_timeout` (min 30 min) back to `skipped`. Catches the SIGKILL / OOM / container-restart case where the in-worker try/except can't run.

**Skill Worker** (`SkillWorkerSettings`):
- `process_skill_task` — process uploaded skill packages

### MRP Pipeline (`app/ai/mrp/`)
The core compilation pipeline — MAP-REDUCE-PLAN-REFINE-VERIFY:

1. **Triage + MAP** (`mapper.py`) — classifies document size (single_pass / standard / hierarchical), splits into ~20K-char chunks aligned to section headings, runs parallel LLM extraction per chunk. Each chunk produces structured JSON (entities, concepts, claims with `absolute_offset` back to source). Results saved to `source_chunk_extracts` incrementally for crash resume.

2. **REDUCE** (`reducer.py`) — merges all chunk extracts: exact entity dedup → embedding cosine similarity dedup (auto-merge > 0.90, LLM disambiguates 0.75–0.90) → KB reconciliation via semantic search → single planning LLM call → `SourceCompilationPlan` saved with `status=pending_review`.

3. **Human review (Phase 2.5)** — editor approves/modifies/rejects the plan via portal or API. Controlled by `mrp_auto_approve_plan` config for automated pipelines.

4. **REFINE** (`writer.py`) — parallel page writers (up to 4 concurrent). Simple writer (≤ 8 evidence items): 1 LLM call. Complex writer: mini agent loop (max 10 steps, `read_kb_page` / `read_source_excerpt` / `finish` tools). All claims cited with `[^N]` footnotes.

5. **VERIFY** (`verifier.py`) — citation verification (LLM checks each `[^N]` claim against source excerpt), coverage check (entities with ≥ 3 mentions not covered by any page), conflict check (semantic similarity + LLM contradiction detection against existing KB). All checks non-blocking.

6. **COMMIT** (`pipeline.py`) — `apply_create` / `apply_update` per page, `upsert_page_embedding`, single atomic `session.commit()`, `regenerate_index`, `append_log`.

---

## Data model

### Core entities

```
sources                    → uploaded documents (files or URLs)
  ├── source_departments       → which departments can access a source
  ├── source_chunk_extracts    → MAP output: per-chunk structured extraction (entities/claims/concepts)
  └── source_compilation_plans → REDUCE output: planned pages to create/update (pending_review → approved → done)

wiki_pages                 → compiled wiki articles
  ├── wiki_links           → [[wikilink]] graph edges between pages
  ├── wiki_page_drafts     → pending edits / proposed new pages (draft_kind = edit | create)
  │   └── wiki_draft_rounds → snapshots of each (resubmit) cycle: content_md + ai_check_results
  └── wiki_page_revisions  → full version history (immutable snapshots)

knowledge_types            → categories (SOP, Product, HR Policy, ...)

departments                → org units for access scoping
employees                  → user accounts
  │   └── mcp_token_hash / _prefix / _rotated_at  (plaintext never stored)
  └── roles                → custom RBAC role with permission list

projects                   → workspaces (cross-functional contexts)
  ├── project_members      → workspace members with roles (viewer/contributor/editor/admin)
  └── project_sources      → sources linked to a workspace

notifications              → in-app notifications (draft submitted/approved/rejected, etc.)

skills                     → AI skill packages
  ├── skill_departments    → department scoping for skills
  ├── skill_contributions  → contributor uploads pending admin review
  └── skill_versions       → version history with storage paths

stats_daily_metrics        → admin Statistics rollups (one row per day per metric per dim hash)
mcp_query_log              → per-call MCP audit trail (tool name, employee, latency, result count)

audit_log                  → immutable activity log
```

**Key fields on `sources` for pipeline tracking:**
- `pipeline_strategy` — `single_pass | standard | hierarchical` (set by Phase 0 triage)
- `pipeline_phase` — `map | reduce | plan_review | refine | verify | commit` (drives crash resume)
- `status` — includes `plan_ready` (waiting for human review) in addition to `pending | processing | ready | error`

### Permission scoping

Every resource has a scope:
- `scope_type = "global"` — visible to anyone with the appropriate global permission
- `scope_type = "project", scope_id = <project_id>` — restricted to workspace members

This applies to: `sources`, `wiki_pages`, `skills`.

---

## Request flow

### Document upload → wiki compilation

```
POST /api/sources/upload
  → MinIO: store file
  → DB: create Source(status=pending)
  → Redis: enqueue ingest_file_task

Worker: ingest_file_task
  → Extract text (pdfplumber / python-docx / html2text)
  → Vision model: caption embedded images (optional)
  → DB: Source(status=processing)
  → Redis: enqueue ingest_map_reduce_task

Worker: ingest_map_reduce_task   [Phase 0-2]
  → Phase 0 (Triage): classify strategy (single_pass / standard / hierarchical)
  → Phase 1 (MAP): chunk by section headings → parallel LLM extraction
      Each chunk → SourceChunkExtract(status=done) saved incrementally
  → Phase 2 (REDUCE): dedup entities → reconcile with KB → planning LLM call
      → SourceCompilationPlan(status=pending_review)
      → Source(status=plan_ready)   [if manual review]
      → Redis: enqueue ingest_refine_task   [if mrp_auto_approve_plan=True]

[Human review: GET /api/sources/{id}/plan → POST /api/sources/{id}/plan/approve]

Worker: ingest_refine_task   [Phase 3-5]
  → Phase 3 (REFINE): parallel page writers
      Simple (≤8 evidence): 1 LLM call
      Complex (>8 evidence): mini agent loop (read_kb_page, read_source_excerpt, finish)
  → Phase 4 (VERIFY): citation check + coverage check + conflict check (non-blocking)
  → Phase 5 (COMMIT): apply_create / apply_update per page
      Each page → upsert_page_embedding
      DB: Source(status=ready), SourceCompilationPlan(status=done)
      wiki_service.regenerate_index() + append_log()
```

### Employee Claude query → MCP response

```
Claude Desktop → POST /mcp (Bearer ark_xxx)
  → MCPAuthService.verify_token() → resolve employee identity + scope
  → Tool called (e.g. search_wiki)
      → filter wiki pages by employee's allowed knowledge types + dept
      → semantic search via pgvector
  → Return ranked results
```

### Wiki draft workflow

```
Contributor → POST /api/wiki/pages/{slug}/drafts
  → WikiPageDraft(status=pending, ai_check_status=queued) created
  → notify_submitted: insert Notification rows for every reviewer-in-scope
  → enqueue ai_pre_review_draft_task(draft_id, revision_round)

Worker: ai_pre_review_draft_task
  → L1 regex + L2 structural + L3 semantic + L4 LLM checks
  → refresh(draft) and bail if revision_round bumped (resubmit race guard)
  → write ai_check_results, ai_check_status ∈ {passed, warned, failed}

Editor → GET /api/wiki/drafts (lists pending in their scope)
       → /wiki/review console (3-pane) for high-volume review

Editor → POST /api/wiki/drafts/{id}/approve     (advisory lock on hashtext(slug))
  → page row refreshed inside critical section
  → WikiPage.content_md updated, version++
  → WikiPageRevision(change_type=draft_approved) created
  → notify_approved: author + every sibling-draft author on the same page

Editor → POST /api/wiki/drafts/{id}/reject       (reviewer_note required)
       → POST /api/wiki/drafts/{id}/request-changes  (reviewer_note required)
                → status=needs_revision

Author → PATCH /api/wiki/drafts/{id}/content    (resubmit after request_changes)
       → wiki_draft_rounds snapshot of previous content + verdict
       → revision_round++
       → status=pending, ai_check_status=pending, enqueue new AI worker

Author → POST /api/wiki/drafts/{id}/withdraw    (pending or needs_revision)
       → status=withdrawn (terminal)
```

---

## Directory structure

```
arkon/
├── app/
│   ├── main.py               # FastAPI app, CORS, router registration, lifespan
│   ├── config.py             # Settings (pydantic-settings, reads from .env)
│   ├── database/
│   │   ├── models.py         # SQLAlchemy ORM models
│   │   └── __init__.py       # async_session_factory, get_db dependency
│   ├── routers/
│   │   ├── auth.py           # Login, me, change-password
│   │   ├── sources.py        # Document upload, ingestion, retry
│   │   ├── wiki.py           # Wiki page CRUD, revisions, graph
│   │   ├── wiki_drafts.py    # Draft propose/approve/reject
│   │   ├── projects.py       # Workspace CRUD, members, sources, wiki
│   │   ├── skills.py         # AI skill upload and management
│   │   ├── rbac.py           # Departments, employees
│   │   ├── roles.py          # Role and permission management
│   │   ├── knowledge_types.py
│   │   ├── admin_settings.py # AI provider config
│   │   ├── audit.py          # Audit log
│   │   └── notes.py
│   ├── services/
│   │   ├── auth_service.py       # JWT, get_current_user, require_permission
│   │   ├── mcp_auth_service.py   # MCP token resolution
│   │   ├── permission_engine.py  # RBAC logic, scope resolution
│   │   ├── permissions.py        # Permission string constants
│   │   ├── wiki_service.py       # Wiki CRUD, draft/revision operations
│   │   ├── skill_service.py      # Skill CRUD, versioning
│   │   ├── storage_service.py    # MinIO wrapper
│   │   ├── audit_service.py      # log_audit()
│   │   └── kb_service.py         # Source extraction helpers
│   ├── ai/
│   │   ├── mrp/
│   │   │   ├── mapper.py         # Phase 0 (triage) + Phase 1 (MAP: chunking, extraction)
│   │   │   ├── reducer.py        # Phase 2 (REDUCE: dedup, KB reconcile, planning call)
│   │   │   ├── writer.py         # Phase 3 (REFINE: simple + complex page writers)
│   │   │   ├── verifier.py       # Phase 4 (VERIFY: citation, coverage, conflict checks)
│   │   │   └── pipeline.py       # Phase 5 (COMMIT) + pipeline orchestrators
│   │   ├── wiki_agent.py         # Legacy agent loop (kept for reference)
│   │   ├── wiki_agent_tools.py   # Legacy tool catalog
│   │   ├── wiki_analyzer.py      # Legacy pre-analysis call
│   │   └── providers/            # Provider-agnostic LLM/embedding/vision wrappers
│   └── mcp/
│       ├── server.py             # FastMCP server factory (create_mcp_server)
│       └── tools.py              # All MCP tools (register_tools)
├── frontend/
│   └── src/
│       ├── app/(portal)/         # Page routes (wiki, workspaces, knowledge, ...)
│       └── components/           # UI components
├── alembic/
│   └── versions/                 # Migration files (001 → 020)
├── docker-compose.yml
├── Dockerfile
├── .env.docker.example     # Env template for Docker Compose
├── .env.local.example      # Env template for local development
└── pyproject.toml
```

---

## AI provider support

All AI operations go through provider-agnostic wrappers in `app/ai/providers/`. Configured at runtime via the Admin Portal Settings.

| Capability | Providers |
|---|---|
| **Embedding** | Google (`text-embedding-004`), OpenAI, Voyage, Cohere, Ollama |
| **LLM** | Google (Gemini), OpenAI (GPT), Anthropic (Claude), Ollama |
| **Vision** | Google, OpenAI |

Switching providers requires only a settings change — no code changes.
