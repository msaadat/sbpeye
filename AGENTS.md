# SBPEye

## Purpose

SBPEye is an independent indexer and search engine for State Bank of Pakistan (SBP) circulars and economic data. It scrapes SBP's website, indexes circulars from all departments, and provides a modern web UI with full-text search (hybrid SQLite FTS5 + vector/semantic), browse-by-department/year navigation, PDF previews, AI-powered analysis, and live SBP news.

## Tech Stack

- **Backend**: Python 3.12+, FastAPI (served via Uvicorn)
- **Frontend**: Vue 3 + TypeScript + Vite + Vue Router + Pinia, PrimeVue Aura (SBP green/gold accents, light/dark support)
- **Database**: SQLite via SQLAlchemy ORM (file `sbpeye.db` at project root); also hosts the `circulars_fts` FTS5 virtual table used for keyword search
- **Vector DB**: ChromaDB (persistent, at `chroma_db/`)
- **Search**: Hybrid engine combining a persistent, incrementally-updated SQLite FTS5 keyword index with ChromaDB vector similarity, fused via Reciprocal Rank Fusion (RRF) with title/department match bonuses
- **Scraping**: BeautifulSoup4, requests, pdfplumber (PDF extraction)
- **AI Engine**: Flexible OpenAI-compatible client supporting LM Studio (local), OpenAI API, and Google Gemini. Configured via Settings page or environment variables.
- **CLI**: Click-based command-line interface for syncing, tagging, summarization, etc.
- **Entry point**: `run.py` - runs `uvicorn sbpeye.main:app` on host `0.0.0.0:8000` with reload enabled

## Project Structure

```
SBPEye/
├── run.py                          # Entry point (uvicorn runner)
├── pyproject.toml                  # Project metadata and dependencies
├── sbpeye.db                       # SQLite database file
├── chroma_db/                      # ChromaDB persistent storage
├── frontend/                       # Vue 3 + PrimeVue SPA
│   ├── index.html
│   ├── vite.config.ts
│   ├── tsconfig.json
│   ├── package.json
│   ├── src/
│   │   ├── App.vue
│   │   ├── main.ts
│   │   ├── router/
│   │   ├── stores/
│   │   ├── views/
│   │   ├── components/
│   │   └── assets/
│   └── public/
├── src/sbpeye/
│   ├── __init__.py
│   ├── main.py                     # FastAPI app, all routes, HTML cleaning logic
│   ├── models.py                   # SQLAlchemy models (Circular, ChatSession, Settings, etc.)
│   ├── database.py                 # DB engine, session factory, ChromaDB client, migrations
│   ├── search.py                   # Hybrid search engine (SQLite FTS5 + ChromaDB + RRF)
│   ├── ai.py                       # AI client module (LM Studio, OpenAI, Google Gemini)
│   ├── api/
│   │   ├── __init__.py
│   │   └── serializers.py          # Pure payload/serialization + workspace/settings helpers
│   ├── scraper/
│   │   ├── __init__.py
│   │   ├── circulars.py            # SBP circulars scraper
│   │   ├── laws.py                 # SBP laws & regulations scraper (content-hash versioning)
│   │   ├── ecodata.py              # Economic data scraper
│   │   ├── ecodata_index.py       # EcoData index page scraper
│   │   ├── news.py                 # SBP homepage news scraper
│   │   └── pdf_summarizer.py       # PDF summarization for EcoData
│   ├── cli/
│   │   ├── __init__.py
│   │   └── commands.py             # Click CLI: sync, summarize, tags, checklist, relationships, status
│   └── static/                     # Built Vue SPA served at the root route (static/spa/)
└── tests/                          # pytest suite
```

## Database Models (models.py)

- **Circular** (`circulars`): `id` (UUID5 from URL), `reference`, `title`, `department`, `date`, `url`, `content_text`, `summary`, `tags` (JSON), `compliance_checklist` (JSON), `status` (active/amended/superseded/cancelled)
- **CircularRelationship** (`circular_relationships`): `source_id`, `target_id` (nullable), `target_reference` (raw text), `type` (amends/supersedes/cancels/adds_to/clarifies), `confidence` (float)
- **Settings** (`settings`): `key` (PK), `value` — stores AI config (provider, API key, model, etc.)
- **CircularConsolidation** (`circular_consolidations`): one row per amendment chain, keyed by `chain_id` (the base circular's id); `member_ids` (JSON, date-ordered), `as_of_circular_id`, `requirements` (JSON merged requirement list with per-item provenance and old/new values), `stale` flag (set when a relationships pass touches a chain member). Built by `consolidation.py`.
- **RegDocument** (`reg_documents`): a law/regulation/guideline from SBP's `/laws-regulations` listing. `id` (UUID5 from normalized title), `normalized_title` (identity basis, version suffixes stripped), `doc_type` (law/regulation/guideline/gazette/licensing), `parent_id` (self-referencing — FE Manual chapters and appendices), `part_label`/`part_order`, `circular_id` (set when the listing row is really an indexed circular), `is_external`, `delisted_at` (rows are never deleted). See `docs/LAWS_REGULATIONS_PLAN.md`
- **RegDocumentVersion** (`reg_document_versions`): one captured state per `content_hash` — SBP replaces PDFs in place and keeps no history, so hashes are the only change detector and every fetched file is archived immutably under `attachments/laws/<document_id>/`. `is_current` is decided per document per sync from `effective_from` (parallel editions), not by recency; `source` is live/wayback
- **RegDocumentLink** (`reg_document_links`): circular ↔ regulation edge; `link_type` (amends/annexure_of/references/implements/listing), `detected_via` (url_scan/ai/listing), `confidence`
- **ChatSession** (`chat_sessions`): `id` (UUID), `title`, `created_at`
- **ChatMessage** (`chat_messages`): `id` (UUID), `session_id`, `role`, `content`, `circular_ids` (JSON), `created_at`
- **EcoDataSeries**, **EcoDataEntry**, **EcoDataCache**, **SyncStatus** — unchanged

## AI Engine (ai.py)

Supports three providers via OpenAI-compatible API:
- **LM Studio** (local): `http://localhost:1234/v1`
- **OpenAI**: `https://api.openai.com/v1`
- **Google Gemini**: Uses `generativelanguage.googleapis.com/v1beta/openai/` endpoint

Configuration priority: Settings DB > Environment variables > Defaults

Task methods:
- `summarize()` — 3-5 sentence summary of a circular
- `generate_tags()` — Selects 1-5 tags from predefined taxonomy of ~50 SBP-relevant categories
- `generate_checklist()` — Compliance checklist with action_required flags
- `extract_relationships()` — Identifies amends/supersedes/cancels/adds_to/clarifies references
- `extract_requirements()` / `align_requirements()` — Chain consolidation: extract a base circular's requirement list, then classify what each amending circular changes (modify/add/remove); orchestrated by `consolidation.py`
- `chat()` — Conversational Q&A with circular context
- `test_connection()` — Validates API connectivity

## CLI Commands

```bash
# Scrape circulars from SBP website
sbpeye circulars sync --dept bprd --year 2025 --limit 10 -v

# AI-powered batch processing (one-time, results stored in DB)
sbpeye circulars summarize          # Generate summaries
sbpeye circulars tags               # Assign tags from taxonomy
sbpeye circulars checklist          # Generate compliance checklists
sbpeye circulars relationships      # Extract circular relationships
sbpeye circulars consolidate        # Consolidate amendment chains (merged requirement views)
sbpeye circulars status             # Recompute status from relationships

# Run full pipeline
sbpeye circulars all --dept bprd --year 2025

# Laws & regulations (sbp.org.pk/laws-regulations)
sbpeye laws sync --type regulation -v   # Scrape the listing; new content hash = new version
sbpeye laws backlink [--rescan]         # Link circulars to the regulations they cite (no LLM)
sbpeye laws reindex [--force]           # Rebuild the laws FTS5 + ChromaDB indexes
sbpeye laws status                      # Counts by type, versions, pending extractions

# AI analysis of laws & regulations (docs/LAWS_AI_PLAN.md). Same options as the circular
# commands: --id/--doc-type/--limit/--force/--delay/-v. Results are stored against the
# *version* in force, so a new edition reads as un-analysed rather than showing stale text.
sbpeye laws summarize                   # Summaries; collections roll up their parts
sbpeye laws tags                        # Taxonomy tags (drawn from the summary)
sbpeye laws checklist                   # Cited obligations, page-anchored via Docling
sbpeye laws entities                    # Structured regulatory values (CAR, MCR, limits)
sbpeye laws relationships               # Type law↔law edges and the circulars acting on each law

# Other commands
sbpeye stats                        # Show DB statistics
sbpeye dry-run --dept bprd --year 2025  # Preview what would be scraped

# Options for all AI commands:
--force          # Re-process already-processed circulars
--limit N        # Process only N circulars
--delay SECONDS  # Delay between API calls (default: 1.0)
--verbose / -v   # Print extra details
```

## Routes (main.py)

### Page Routes (SPA — all served from built frontend)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | SPA entry point (Vue Router handles all client-side routes) |

### API Routes
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/circulars/search` | Hybrid search. Query: `q`, `tag`, `department`, `start_year`, `end_year`, `sort_by`, `source` (circulars/laws/all, default circulars), `doc_type` |
| GET | `/api/laws` | List or search laws & regulations. Query: `q`, `doc_type`, `parent_id`, `top_level`, `include_delisted` |
| GET | `/api/laws/types` | Document-type facets with counts |
| GET | `/api/laws/{id}` | Detail: current version, version timeline, parts, linked circulars, AI analysis (`generation`, `entities`, `relationships`) |
| POST | `/api/laws/{id}/generate` | Queue AI analysis for a law. Body `{feature}`; 422 names the reason and whether it is structural |
| GET | `/api/laws/{id}/checklist.xlsx` | Export the in-force edition's obligations checklist as a workbook |
| GET | `/api/laws/{id}/versions/{vid}` | Version detail incl. extracted text and archive reference |
| GET | `/api/laws/{id}/file` | Serve a version's archived file from disk |
| GET | `/api/circulars/departments` | List departments with circular counts |
| GET | `/api/circulars/years` | List years for a department |
| GET | `/api/circulars/browse` | List circulars for dept+year |
| GET | `/api/circulars/browse_recent` | List recent circulars (for chat context) |
| GET | `/api/circulars/by_url` | Find circular by URL |
| GET | `/api/circulars/{id}` | Full circular detail (summary, tags, checklist, relationships) |
| GET | `/api/circulars/{id}/relationships` | Relationship graph for a circular |
| GET | `/api/circulars/tags` | List all tags with counts |
| GET | `/api/ecodata` | Get economic data series |
| GET | `/api/ecodata/entries` | List EcoData entries |
| GET | `/api/ecodata/pdf_summary` | PDF summary for EcoData |
| GET | `/api/sbp_news` | Scrape SBP homepage news |
| GET | `/api/pdf_preview` | PDF preview |
| GET | `/api/circulars/export_csv` | Export search results as CSV |
| POST | `/api/circulars/batch_download` | Download multiple circulars as ZIP |
| POST | `/api/chat` | Send chat message with circular context |
| GET | `/api/chat/sessions` | List chat sessions |
| GET | `/api/chat/sessions/{id}` | Get session messages |
| DELETE | `/api/chat/sessions/{session_id}` | Delete a chat session |
| GET | `/api/settings` | Get current AI settings |
| POST | `/api/settings` | Save AI settings |
| POST | `/api/settings/test` | Test AI connection |

### Admin console (`api/admin.py`)

Admin-gated at the router, and **read-only by design** — every route is a GET that changes
nothing. Nothing here syncs, indexes or generates; those remain CLI commands, largely
because the deployment cannot reach SBP at all (`DEPLOYMENT_PLAN.md` §2.1). The UI is
`/admin`, whose tabs are child routes (`/admin/corpus`, `/admin/index`, `/admin/runs`,
`/admin/users`, `/admin/deployment`) served by the `/admin/{path:path}` SPA fallback.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/admin/corpus` | Corpus composition and AI coverage — what `sbpeye stats` and `laws status` print, structured |
| GET | `/api/admin/index` | Index health as *recorded* by the ledger, plus FTS sizes and embedding-fingerprint drift |
| GET | `/api/admin/index/audit` | Live `reconcile(write=False)` against the vector store — a measurement, never a repair |
| GET | `/api/admin/runs` | Sync runs (both corpora) and AI generation jobs, newest first |
| GET | `/api/admin/environment` | Data root, database and file-tree sizes, and capability flags (docling, vector store, tracing) |
| GET | `/api/admin/users` | User management (in `auth_routes.py`; shares the prefix) |

## Running the Project

```bash
# Install dependencies
uv sync

# Run the server (port 8000, auto-reload)
python run.py

# Or use CLI for batch operations
sbpeye circulars sync --dept bprd --year 2025 -v
sbpeye circulars summarize --dept bprd --limit 10 -v
sbpeye circulars tags --force -v
sbpeye circulars all --dept bprd --year 2025
sbpeye stats
```

### Frontend Development

```bash
cd frontend && npm install    # Install frontend dependencies
cd frontend && npm run dev    # Vite dev server (port 5173, proxies API to FastAPI)
cd frontend && npm run build  # Production build to frontend/dist/
cd frontend && npm run typecheck  # TypeScript type checking
```

## Key Conventions

- Python 3.12+ required
- Dependencies managed with uv (`pyproject.toml`, `uv.lock`)
- DB sessions are managed via FastAPI dependency injection (`get_db` generator)
- Database migrations are handled automatically via `_ensure_columns()` in database.py (no Alembic); it also creates the `circulars_fts` virtual table
- The FTS5 keyword index (`circulars_fts`) is maintained at the application layer, not via SQL triggers — any code path that changes a circular's or attachment's text must call `index_circular_fts(db, circular)` (search.py) alongside the existing ChromaDB write, or the circular won't surface in keyword search
- Laws/regulations are versioned by content hash, never by URL, title or listing date — SBP replaces PDFs in place. Archived files under `attachments/laws/<document_id>/<hash8>-<name>` are immutable: never overwrite one, never delete a delisted document. Which version is in force is decided once per document per sync by `select_current_versions()`, never as a side effect of fetch order — any new code path that captures a version must record it via `_observe()` so the currency tiebreak can see it
- A container document (the FE Manual and its chapters) has no bytes of its own: its version is a **manifest** (`file_type="manifest"`) hashed over its children's hashes, so a new row appears only when a part actually changed, and diffing two manifests says which part moved. Manifests are bookkeeping, not readable text — keep them out of the search index
- Laws and circulars **share the ChromaDB collection** and are kept apart by metadata: law chunks carry `kind="law"`, and the circular vector arm filters `doc_type in (circular, attachment)`. Without that filter law chunks flood the circular candidate set as ids that resolve to no circular (measured: 31 of the top 50 chunks on an FX query) — never query the collection unfiltered
- Only the version **in force** is searchable; superseded versions stay in SQLite and on disk but are dropped from both indexes. Manifests are never indexed. `RegDocumentVersion.is_vectorized` is the ledger that keeps `laws sync` from re-embedding unchanged documents
- Deterministic circular ↔ regulation links live in `laws_links.py` (models + URL helpers only, so both scrapers can import it). Edges found by URL or by name are recorded as `link_type="references"` — asserting that a circular *amends* a regulation is a judgement about meaning, reserved for the AI pass. Name matching uses top-level documents only (a part's title is a subject line like "EXPORTS"); parts are reached via "Chapter 12 of the FE Manual" near a mention of the container
- Some listing rows *are* circulars SBPEye already holds. Those resolve via `find_circular_by_url()` (link_routing.py) to `RegDocument.circular_id` + a `RegDocumentLink`, and store **no content of their own** — never scrape a circular twice into two records that can drift apart. An unresolvable row stays a stub and retries every sync
- `SyncStatus` rows are shared by both scrapers and discriminated by `kind`; every circular-facing query must filter through `models.circular_sync_only()` so laws runs stay out of the circular sync banner
- `chroma_db/` is local runtime data and should not be committed to git.
- Frontend is a Vue 3 SPA with Vue Router, Pinia stores, and PrimeVue components
- Dark mode is implemented via PrimeVue's built-in dark mode class switching
- FastAPI serves the built SPA from `frontend/dist/` at the root route
- PDF links automatically get preview buttons injected by the SPA
- Navigational content from SBP pages is stripped via `clean_sbp_html()` helper in main.py
- AI config: Settings DB takes priority over env vars. Env vars: `AI_PROVIDER`, `AI_BASE_URL`, `AI_API_KEY`, `AI_MODEL`, `AI_CHAT_MODEL`
- Circular scraping is done via CLI (`sbpeye circulars sync`), not from the web UI
- All AI batch operations (summarize, tags, checklist, relationships) are run via CLI and results stored in DB
- The admin console **reports** on all of the above and starts none of it. Reading corpus or index state must never mutate either — the audit route calls `reconcile(write=False)` for exactly that reason, and `tests/test_admin_status.py` asserts it. A new admin route that writes belongs behind a POST and an explicit operator action, not on a page that reloads