# SBPEye

## Purpose

SBPEye is an independent indexer and search engine for State Bank of Pakistan (SBP) circulars and economic data. It scrapes SBP's website, indexes circulars from all departments, and provides a modern web UI with full-text search (hybrid BM25 + vector/semantic), browse-by-department/year navigation, PDF previews, AI-powered analysis, and live SBP news.

## Tech Stack

- **Backend**: Python 3.12+, FastAPI (served via Uvicorn)
- **Templates**: Jinja2 (with HTMX partial rendering)
- **Frontend**: HTMX 2.0 for interactivity, Alpine.js for reactive state, Tailwind CSS via CDN, Lucide icons
- **Database**: SQLite via SQLAlchemy ORM (file `sbpeye.db` at project root)
- **Vector DB**: ChromaDB (persistent, at `chroma_db/`)
- **Search**: Hybrid engine combining BM25 (rank-bm25) keyword ranking with ChromaDB vector similarity, fused via Reciprocal Rank Fusion (RRF) with title/department match bonuses
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
├── src/sbpeye/
│   ├── __init__.py
│   ├── main.py                     # FastAPI app, all routes, HTML cleaning logic
│   ├── models.py                   # SQLAlchemy models (Circular, ChatSession, Settings, etc.)
│   ├── database.py                 # DB engine, session factory, ChromaDB client, migrations
│   ├── search.py                   # Hybrid search engine (BM25 + ChromaDB + RRF)
│   ├── ai.py                       # AI client module (LM Studio, OpenAI, Google Gemini)
│   ├── scraper/
│   │   ├── __init__.py
│   │   ├── circulars.py            # SBP circulars scraper
│   │   ├── ecodata.py              # Economic data scraper
│   │   ├── ecodata_index.py       # EcoData index page scraper
│   │   ├── llm.py                  # LLM helper (delegates to ai.py)
│   │   └── pdf_summarizer.py       # PDF summarization for EcoData
│   ├── cli/
│   │   ├── __init__.py
│   │   └── commands.py             # Click CLI: sync, summarize, tags, checklist, relationships, status
│   ├── templates/
│   │   ├── _base.html              # Base layout (nav, dark mode, PDF modal)
│   │   ├── index.html              # Main dashboard page
│   │   ├── circular.html           # Circular view page (with AI analysis)
│   │   ├── chat.html               # Chat with circulars page
│   │   ├── settings.html           # AI settings page
│   │   ├── ecodata.html            # EcoData page
│   │   └── partials/
│   │       ├── news.html           # HTMX partial: SBP press releases + what's new
│   │       ├── departments.html    # HTMX partial: department cards grid
│   │       ├── years.html          # HTMX partial: year cards grid
│   │       ├── circulars.html     # HTMX partial: circular list
│   │       ├── search_results.html # HTMX partial: search results with status/tags
│   │       ├── ecodata_table.html # HTMX partial: ecodata table
│   │       └── ecodata_summary.html # HTMX partial: PDF summary
│   └── static/
│       ├── app.js                  # Client JS: PDF preview, HTMX hooks, icon injection
│       ├── tailwind.css            # Tailwind CSS custom styles
│       └── favicon.svg
└── test_circulars.py               # Legacy test harness (use CLI instead)
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

### Page Routes
| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Main dashboard with news, browse/search tabs |
| GET | `/view_circular` | View circular with AI analysis (summary, tags, checklist, relationships) |
| GET | `/ecodata` | EcoData page |
| GET | `/chat` | Chat with circulars page |
| GET | `/settings` | AI settings page (provider, API key, model config) |

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
| POST | `/api/settings` | Save AI settings |
| POST | `/api/settings/test` | Test AI connection |
| POST | `/api/chat` | Send chat message with circular context |
| GET | `/api/chat/sessions` | List chat sessions |
| GET | `/api/chat/sessions/{id}` | Get session messages |

### HTMX Partial Routes
| Method | Path | Description |
|--------|------|-------------|
| GET | `/partials/news` | News cards |
| GET | `/partials/departments` | Department cards grid |
| GET | `/partials/years` | Year cards for a department |
| GET | `/partials/circulars` | Circular list |
| GET | `/partials/search` | Search results with pagination. Supports `tag` filter |
| GET | `/partials/ecodata_table` | EcoData table |
| GET | `/partials/ecodata_summary` | PDF summary modal content |

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

## Key Conventions

- Python 3.12+ required
- Dependencies managed with uv (`pyproject.toml`, `uv.lock`)
- DB sessions are managed via FastAPI dependency injection (`get_db` generator)
- Database migrations are handled automatically via `_ensure_columns()` in database.py (no Alembic)
- Templates use Jinja2 inheritance: `_base.html` is the parent, all pages extend it
- Dark mode is implemented via Alpine.js + class-based Tailwind dark mode with localStorage persistence
- Frontend uses HTMX for AJAX partials (hx-get/hx-target/hx-trigger/hx-swap/hx-indicator patterns)
- PDF links automatically get preview buttons injected by `app.js` MutationObserver
- Navigational content from SBP pages is stripped via `clean_sbp_html()` helper in main.py
- AI config: Settings DB takes priority over env vars. Env vars: `AI_PROVIDER`, `AI_BASE_URL`, `AI_API_KEY`, `AI_MODEL`, `AI_CHAT_MODEL`
- Circular scraping is done via CLI (`sbpeye circulars sync`), not from the web UI
- All AI batch operations (summarize, tags, checklist, relationships) are run via CLI and results stored in DB