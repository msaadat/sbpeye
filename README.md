# SBPEye

SBPEye is an independent indexer and search engine for State Bank of Pakistan (SBP) circulars and economic data.
It scrapes SBP pages, stores circulars and economic datasets, and provides a modern web UI with hybrid search, AI-powered analysis, PDF preview, and chat.

## Key Features

- Scrapes SBP circulars and EcoData content
- Stores data in SQLite with persistent ChromaDB vector embeddings
- Hybrid search combining BM25 keyword ranking with vector similarity
- AI-driven summary, tagging, compliance checklists, and relationship extraction
- HTMX + Alpine.js front-end for fast interactive browsing and search
- Localizable AI backend supporting LM Studio, OpenAI, and Google Gemini
- CLI for batch sync, summarization, tagging, relationships, and status updates

## Tech Stack

- Python 3.12+
- FastAPI + Uvicorn
- Jinja2 templates
- HTMX, Alpine.js, Tailwind CSS
- SQLite via SQLAlchemy
- ChromaDB for embeddings
- BeautifulSoup4, requests, pdfplumber for scraping
- Click for CLI

## Getting Started

### Requirements

- Python 3.12+
- `uv` for dependency management if using the supplied lockfile

### Install

```bash
cd /home/saad/Work/SBPEye
uv sync
```

### Run the app

```bash
python run.py
```

Then open `http://localhost:8000`.

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
    ├── cli/
    │   └── commands.py
    ├── templates/
    │   ├── _base.html
    │   ├── index.html
    │   ├── circular.html
    │   ├── chat.html
    │   ├── settings.html
    │   ├── ecodata.html
    │   └── partials/
    └── static/
        ├── app.js
        ├── tailwind.css
        └── favicon.svg
```

## Notes

- `uv.lock` is the committed lockfile for dependency reproducibility when using `uv`.
- `.python-version` is optional and only required if using `pyenv` or similar local version managers.
- Scraping is driven by the CLI; the web UI is for browsing, searching, and AI analysis.

## License

Add license details here if needed.
