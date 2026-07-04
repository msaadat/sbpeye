import cloudscraper
import pdfplumber
from io import BytesIO
from datetime import datetime
from sqlalchemy.orm import Session
from markdown_it import MarkdownIt
from ..models import EcoDataCache

md = MarkdownIt().enable("table")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# On the redesigned site, economic-data documents live under the shared /assets store
# instead of /ecodata/*.pdf, so summarizability is decided by path + type, not an
# exact-URL allowlist.
ECODATA_DOC_PREFIXES = (
    "https://www.sbp.org.pk/assets/document/",
    "https://www.sbp.org.pk/assets/documents/",
)
# Filename keyword -> specialized parser; every other economic-data PDF uses the generic one.
SUMMARY_KINDS = {
    "gdp_table": "gdp",
    "qgdp": "qgdp",
}


def _summary_kind(url: str) -> str:
    name = url.rsplit("/", 1)[-1].lower()
    for keyword, kind in SUMMARY_KINDS.items():
        if keyword in name:
            return kind
    return "generic"


def is_summarizable(url: str) -> bool:
    lowered = (url or "").lower()
    return lowered.endswith(".pdf") and lowered.startswith(ECODATA_DOC_PREFIXES)

def _download_pdf(url: str) -> bytes:
    resp = cloudscraper.create_scraper().get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.content

def _table_to_markdown(table: list[list]) -> str:
    if not table or len(table) < 2:
        return ""

    cleaned = []
    for row in table:
        cleaned_row = []
        for cell in row:
            if cell is None:
                cleaned_row.append("")
            else:
                cleaned_row.append(str(cell).replace("\n", " ").strip())
        cleaned.append(cleaned_row)

    if not any(any(cell for cell in row) for row in cleaned):
        return ""

    max_cols = max(len(row) for row in cleaned)
    for row in cleaned:
        while len(row) < max_cols:
            row.append("")

    header = cleaned[0]
    md_lines = []
    md_lines.append("| " + " | ".join(header) + " |")
    md_lines.append("| " + " | ".join(["---"] * max_cols) + " |")

    for row in cleaned[1:]:
        md_lines.append("| " + " | ".join(row) + " |")

    return "\n".join(md_lines)

def _generic_summarize(url: str) -> str:
    pdf_bytes = _download_pdf(url)
    pdf = pdfplumber.open(BytesIO(pdf_bytes))

    sections = []
    max_pages = min(3, len(pdf.pages))

    for i in range(max_pages):
        page = pdf.pages[i]
        text = page.extract_text() or ""

        tables = page.extract_tables()

        if tables:
            for j, table in enumerate(tables):
                md_table = _table_to_markdown(table)
                if md_table:
                    sections.append(f"### Page {i + 1} - Table {j + 1}\n\n{md_table}")
        elif text.strip():
            lines = text.strip().split("\n")[:20]
            sections.append(f"### Page {i + 1}\n\n" + "\n".join(lines))

    pdf.close()

    if not sections:
        return "No extractable content found in this PDF."

    return "\n\n".join(sections)

def _parse_gdp(url: str) -> str:
    pdf_bytes = _download_pdf(url)
    pdf = pdfplumber.open(BytesIO(pdf_bytes))

    sections = []

    for i, page in enumerate(pdf.pages[:3]):
        text = page.extract_text() or ""
        if text.strip():
            lines = text.strip().split("\n")
            title_line = lines[0] if lines else "GDP Data"
            sections.append(f"### {title_line}\n")

        tables = page.extract_tables()
        for table in tables:
            if table and len(table) > 1:
                md_table = _table_to_markdown(table)
                if md_table:
                    sections.append(md_table)
                    sections.append("")

    pdf.close()

    if not sections:
        return _generic_summarize(url)

    return "\n".join(sections)

def _parse_qgdp(url: str) -> str:
    pdf_bytes = _download_pdf(url)
    pdf = pdfplumber.open(BytesIO(pdf_bytes))

    sections = []

    for i, page in enumerate(pdf.pages[:3]):
        text = page.extract_text() or ""
        if text.strip():
            lines = text.strip().split("\n")
            title_line = lines[0] if lines else "Quarterly GDP Data"
            sections.append(f"### {title_line}\n")

        tables = page.extract_tables()
        for table in tables:
            if table and len(table) > 1:
                md_table = _table_to_markdown(table)
                if md_table:
                    sections.append(md_table)
                    sections.append("")

    pdf.close()

    if not sections:
        return _generic_summarize(url)

    return "\n".join(sections)

SPECIALIZED_PARSERS = {
    "gdp": _parse_gdp,
    "qgdp": _parse_qgdp,
}

def summarize_pdf(url: str, db: Session) -> str:
    if not is_summarizable(url):
        return "This document is not configured for summarization."

    cached = db.query(EcoDataCache).filter(EcoDataCache.url == url).first()
    if cached:
        return md.render(cached.summary_markdown)

    parser_type = _summary_kind(url)

    if parser_type in SPECIALIZED_PARSERS:
        summary = SPECIALIZED_PARSERS[parser_type](url)
    else:
        summary = _generic_summarize(url)

    cache_entry = EcoDataCache(
        url=url,
        summary_markdown=summary,
        created_at=datetime.now()
    )
    db.add(cache_entry)
    db.commit()

    return md.render(summary)
