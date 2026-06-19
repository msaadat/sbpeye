# SBPEye

SBPEye is an independent indexer and search engine for State Bank of Pakistan (SBP) circulars and economic data.
It scrapes SBP pages, stores circulars and economic datasets, and provides a modern web UI with hybrid search, AI-powered analysis, PDF preview, and chat.

## Key Features

- Scrapes SBP circulars and EcoData content
- Stores data in SQLite with persistent ChromaDB vector embeddings
- Hybrid search combining BM25 keyword ranking with vector similarity
- AI-driven summary, tagging, compliance checklists, and relationship extraction
- Vue 3 + PrimeVue SPA front-end for fast interactive browsing and search
- Localizable AI backend supporting LM Studio, OpenAI, and Google Gemini
- CLI for batch sync, summarization, tagging, relationships, and status updates

## Tech Stack

- Python 3.12+
- FastAPI + Uvicorn
- Vue 3 + TypeScript + Vite + Vue Router + Pinia
- PrimeVue Aura (SBP green/gold accents, light/dark support)
- SQLite via SQLAlchemy
- ChromaDB for embeddings
- BeautifulSoup4, requests, pdfplumber for scraping
- Click for CLI

## Getting Started

### Requirements

- Python 3.12+
- `uv` for dependency management if using the supplied lockfile
- Node.js 18+ and npm (for frontend)

### Install

```bash
# Backend dependencies
uv sync

# Frontend dependencies
cd frontend && npm install
```

### Run the app (development)

```bash
# Terminal 1: Backend (FastAPI)
python run.py

# Terminal 2: Frontend dev server (Vite)
cd frontend && npm run dev
```

Then open `http://localhost:5173` (Vite dev server proxies API to FastAPI).

### Build frontend for production

```bash
cd frontend && npm run build
```

FastAPI serves the built SPA from `frontend/dist/` at `http://localhost:8000`.

The `chroma_db/` directory is generated locally and is not tracked in git. If it is missing in a fresh clone, the app will still start; run `sbpeye reindex` after syncing circulars to rebuild the vector store.

## AI Configuration

AI settings can be provided through the app's settings page or environment variables.
Supported providers:

- LM Studio (local): `http://localhost:1234/v1`
- OpenAI
- Google Gemini (`generativelanguage.googleapis.com/v1beta/openai/`)

Environment variables:

- `AI_PROVIDER`
- `AI_BASE_URL`
- `AI_API_KEY`
- `EMBEDDING_PROVIDER` (`fastembed` by default, or `lmstudio`)
- `EMBEDDING_MODEL` (defaults to `BAAI/bge-base-en-v1.5`)
- `EMBEDDING_BASE_URL` (defaults to `http://localhost:1234/v1`)
- `EMBEDDING_API_KEY` (defaults to `lm-studio`)
- `AI_MODEL`
- `AI_CHAT_MODEL`

## CLI Usage

The package exposes a CLI entry point as `sbpeye`.

Example commands:

```bash
sbpeye circulars sync --dept bprd --year 2025 --limit 10 -v
sbpeye circulars summarize
sbpeye circulars tags
sbpeye circulars checklist
sbpeye circulars relationships
sbpeye circulars status
sbpeye circulars all --dept bprd --year 2025
sbpeye stats
sbpeye dry-run --dept bprd --year 2025
```

Common options:

- `--force` to reprocess existing items
- `--limit N` to restrict processing
- `--delay SECONDS` to throttle API calls
- `--verbose` / `-v`

## Project Layout

```
SBPEye/
├── run.py
├── pyproject.toml
├── uv.lock
├── sbpeye.db
├── chroma_db/
├── frontend/                        # Vue 3 + PrimeVue SPA
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
└── src/sbpeye/
    ├── __init__.py
    ├── main.py
    ├── models.py
    ├── database.py
    ├── search.py
    ├── ai.py
    ├── scraper/
    │   ├── circulars.py
    │   ├── ecodata.py
    │   ├── ecodata_index.py
    │   ├── llm.py
    │   └── pdf_summarizer.py
    └── cli/
        └── commands.py
```

## Notes

- `uv.lock` is the committed lockfile for dependency reproducibility when using `uv`.
- `.python-version` is optional and only required if using `pyenv` or similar local version managers.
- Scraping is driven by the CLI; the web UI is for browsing, searching, and AI analysis.
- Frontend typecheck: `cd frontend && npm run typecheck`

## Development Commands

| Task | Command |
|------|---------|
| Backend server | `python run.py` |
| Frontend dev server | `cd frontend && npm run dev` |
| Frontend build | `cd frontend && npm run build` |
| Frontend typecheck | `cd frontend && npm run typecheck` |

## License

Add license details here if needed.
