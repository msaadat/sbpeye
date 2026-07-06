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
sbpeye circulars status             # Recompute status from relationships

# Run full pipeline
sbpeye circulars all --dept bprd --year 2025

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
| GET | `/api/circulars/search` | Hybrid search. Query: `q`, `tag`, `department`, `start_year`, `end_year`, `sort_by` |
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
- `chroma_db/` is local runtime data and should not be committed to git.
- Frontend is a Vue 3 SPA with Vue Router, Pinia stores, and PrimeVue components
- Dark mode is implemented via PrimeVue's built-in dark mode class switching
- FastAPI serves the built SPA from `frontend/dist/` at the root route
- PDF links automatically get preview buttons injected by the SPA
- Navigational content from SBP pages is stripped via `clean_sbp_html()` helper in main.py
- AI config: Settings DB takes priority over env vars. Env vars: `AI_PROVIDER`, `AI_BASE_URL`, `AI_API_KEY`, `AI_MODEL`, `AI_CHAT_MODEL`
- Circular scraping is done via CLI (`sbpeye circulars sync`), not from the web UI
- All AI batch operations (summarize, tags, checklist, relationships) are run via CLI and results stored in DB