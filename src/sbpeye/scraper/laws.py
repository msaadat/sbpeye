"""Scraper for SBP's laws & regulations listing (https://www.sbp.org.pk/laws-regulations).

Phase 2 of docs/LAWS_REGULATIONS_PLAN.md: the listing's flat, directly-linked documents.

Unlike circulars — immutable dated events — these are living documents. SBP replaces the
PDF in place at the same URL and keeps no history, so nothing about a URL, title or listing
date is a reliable change signal; only the content hash is. Every distinct hash becomes a
`RegDocumentVersion` archived immutably under `attachments/laws/`, which makes SBPEye the
historical record the site itself does not keep.

Rows that link to a subpage or to a circular are recorded here as metadata-only stubs;
phases 3 and 4 give them content.
"""

import hashlib
import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from ..database import PROJECT_ROOT
from ..link_routing import DOCUMENT_EXTENSIONS, find_circular_by_url, is_allowed_sbp_url
from ..models import RegDocument, RegDocumentLink, RegDocumentVersion
from .circulars import (
    ATTACHMENTS_DIR,
    BASE_URL,
    HEADERS,
    _content_matches_file_type,
    _get_sbp,
    _replace_document_chunks,
    extract_document_text,
    fetch_page_cached,
)
from .clean_html import extract_sbp_text
from ..search import NON_TEXT_LAW_FILE_TYPES, index_law_fts

LAWS_LISTING_URL = f"{BASE_URL}/laws-regulations"
LAWS_ARCHIVE_DIR = ATTACHMENTS_DIR / "laws"

# The listing's `data-type` attribute (also the values in its "Filter By Type" panel)
# mapped to the `RegDocument.doc_type` vocabulary. Gazette Notifications and Licensing
# Guidelines are real sections that currently render zero rows.
DOC_TYPES = {
    "laws": "law",
    "regulations": "regulation",
    "gazette notifications": "gazette",
    "guidelines": "guideline",
    "licensing guidelines": "licensing",
}

# Link destinations a listing row can point at (§1.2 of the plan). Only `pdf` and
# `external` are acted on in phase 2.
ROUTE_PDF = "pdf"
ROUTE_SUBPAGE = "subpage"
ROUTE_CIRCULAR = "circular"
ROUTE_EXTERNAL = "external"
ROUTE_UNKNOWN = "unknown"


# --------------------------------------------------------------------------- titles

# A title's trailing "(Updated till June 2024)" / ", updated till October 07, 2024" is
# version metadata, not identity: the same document keeps the same suffix slot and only
# changes what is inside it. "(being updated)" is a status marker SBP adds and removes on
# externally-hosted laws, so it is stripped for the same reason.
_VERSION_PHRASE_RE = re.compile(
    r"^(?:"
    r"(?:re-?)?updated?(?:\s+(?:on|till|to|up\s+to|as\s+of|through))?\b.*"
    r"|as\s+of\b.*"
    r"|as\s+(?:modified|amended|revised)(?:\s+up)?\s+to\b.*"
    r"|(?:to\s+be\s+)?(?:applicable|effective|in\s+force)\s+from\b.*"
    r"|being\s+updated"
    r"|w\.e\.f\.?\b.*"
    r")$",
    re.IGNORECASE,
)
_PAREN_SUFFIX_RE = re.compile(r"\s*\(([^()]*)\)\s*$")
_DELIMITER_RE = re.compile(r"\s*[,;-]\s*")


def _tidy(text: str) -> str:
    """Collapse the incidental typographic variation SBP titles churn on."""
    text = (text or "").replace("’", "'").replace("‘", "'")
    text = re.sub(r"[–—]", "-", text)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s*/\s*", "/", text)


def _split_once(stem: str) -> tuple[str, str] | None:
    """Strip one version trailer off a title, or return None if it has none."""
    paren = _PAREN_SUFFIX_RE.search(stem)
    if paren and _VERSION_PHRASE_RE.match(paren.group(1).strip()):
        return stem[: paren.start()].strip(), paren.group(1).strip()
    # The comma/dash form: "PRs for SME Financing, updated till October 07, 2024". The
    # trailer contains commas of its own, so split at the earliest delimiter whose whole
    # remainder reads as version metadata rather than at the last comma.
    for delimiter in _DELIMITER_RE.finditer(stem):
        candidate = stem[delimiter.end():].strip()
        if candidate and _VERSION_PHRASE_RE.match(candidate):
            return stem[: delimiter.start()].strip(), candidate
    return None


def _split_version_suffix(title: str) -> tuple[str, str | None]:
    """Split a title into (stem, version suffix); the reported suffix is the outermost.

    Handles the parenthesized form and the comma/dash-led form, repeatedly, so a title
    carrying two trailers loses both from its identity while still reporting one label.
    """
    stem = _tidy(title)
    suffix: str | None = None
    while True:
        split = _split_once(stem)
        if split is None:
            return stem.rstrip(" .,-"), suffix
        stem, candidate = split
        suffix = suffix if suffix is not None else candidate


def normalize_law_title(title: str) -> str:
    """The identity basis for a document: its title minus version metadata, casefolded.

    Years that name the document ("Credit Bureau Act 2015") are part of its identity and
    are kept — only the version suffix slot is stripped, so a future "(Updated 2027)"
    edition still resolves to the same `RegDocument`.
    """
    stem, _ = _split_version_suffix(title)
    return stem.casefold()


def parse_version_label(title: str) -> str | None:
    """The raw version suffix from a title, e.g. "Updated till June 2024"."""
    _, suffix = _split_version_suffix(title)
    return suffix


_MONTHS = {
    month.lower(): index
    for index, month in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"],
        start=1,
    )
}
_MONTH_ALTS = "|".join(_MONTHS)
_DATE_PATTERNS = (
    re.compile(rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month>{_MONTH_ALTS})[a-z]*\.?,?\s+(?P<year>(?:19|20)\d{{2}})\b", re.IGNORECASE),
    re.compile(rf"\b(?P<month>{_MONTH_ALTS})[a-z]*\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<year>(?:19|20)\d{{2}})\b", re.IGNORECASE),
    re.compile(rf"\b(?P<month>{_MONTH_ALTS})[a-z]*\.?,?\s+(?P<year>(?:19|20)\d{{2}})\b", re.IGNORECASE),
)


def _parse_title_date(text: str | None) -> datetime | None:
    """Parse a date out of a title fragment; a bare month means its first day."""
    if not text:
        return None
    for pattern in _DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        groups = match.groupdict()
        try:
            return datetime(
                int(groups["year"]),
                _MONTHS[_month_key(groups["month"])],
                int(groups.get("day") or 1),
            )
        except (KeyError, ValueError):
            continue
    return None


def _month_key(month: str) -> str:
    """Resolve a possibly-abbreviated month name ("Sept", "Oct.") to its full key."""
    lowered = month.lower().rstrip(".")
    if lowered in _MONTHS:
        return lowered
    for name in _MONTHS:
        if name.startswith(lowered):
            return name
    raise KeyError(month)


_EFFECTIVE_FROM_RE = re.compile(
    r"(?:to\s+be\s+)?(?:applicable|effective|in\s+force)\s+(?:from|w\.e\.f\.?)\s*(?P<date>.+)",
    re.IGNORECASE,
)


def parse_effective_from(title: str) -> datetime | None:
    """The date a title says the document takes effect, e.g. "(to be applicable from
    January 1, 2026)".

    This is what separates two live editions of the same document: an edition whose
    effective date has not arrived is captured but stays out of force. "Updated till
    <date>" is deliberately *not* an effective date — it labels a revision that is
    already in force.
    """
    suffix = parse_version_label(title)
    match = _EFFECTIVE_FROM_RE.search(suffix or "")
    return _parse_title_date(match.group("date")) if match else None


def law_identity(title: str) -> str:
    """The stable primary-key id for a top-level laws/regulations document."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sbp-law:{normalize_law_title(title)}"))


# --------------------------------------------------------------------------- listing


def route_link(url: str | None) -> str:
    """Classify a listing row's destination (§1.2 of the plan).

    `pdf` covers any directly-linked document file, not only PDFs — the extension set is
    the same one circular attachments use.
    """
    if not url:
        return ROUTE_UNKNOWN
    if not is_allowed_sbp_url(url):
        return ROUTE_EXTERNAL
    path = urlparse(url).path
    if Path(path).suffix.lower() in DOCUMENT_EXTENSIONS:
        return ROUTE_PDF
    if re.match(r"^/circulars/.+", path):
        return ROUTE_CIRCULAR
    if re.match(r"^/laws-regulations/.+", path):
        return ROUTE_SUBPAGE
    return ROUTE_UNKNOWN


def _row_url(row, base_url: str) -> str | None:
    """The single destination of a listing row.

    Rows carry the same href twice (an icon link and a "View details" link), so the
    first resolvable one wins.
    """
    for anchor in row.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if href and not href.startswith(("#", "javascript:", "mailto:")):
            return urljoin(base_url, href)
    return None


def _parse_listed_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d")
    except ValueError:
        return None


def parse_listing(soup: BeautifulSoup, base_url: str = LAWS_LISTING_URL) -> list[dict]:
    """Parse the listing page into row descriptors, in page order.

    Every item is a `tr.law-row` carrying its metadata in data-attributes, so parsing
    survives layout changes. The same document can legitimately appear on two rows (two
    live editions) — rows are returned as found and grouped by identity later.
    """
    rows: list[dict] = []
    for order, element in enumerate(soup.select("tr.law-row")):
        raw_title = _tidy(element.get("data-title") or element.get_text(" "))
        if not raw_title:
            continue
        raw_type = _tidy(element.get("data-type") or "").casefold()
        url = _row_url(element, base_url)
        rows.append({
            "title": raw_title,
            "normalized_title": normalize_law_title(raw_title),
            "id": law_identity(raw_title),
            "doc_type": DOC_TYPES.get(raw_type, raw_type or None),
            "url": url,
            "route": route_link(url),
            "listed_date": _parse_listed_date(element.get("data-date")),
            "version_label": parse_version_label(raw_title),
            "effective_from": parse_effective_from(raw_title),
            "order": order,
        })
    return rows


def fetch_listing(force: bool = False) -> list[dict]:
    """Fetch (or reuse the disk-cached) listing page and parse its rows."""
    html = fetch_page_cached(LAWS_LISTING_URL, force=force)
    return parse_listing(BeautifulSoup(html, "html.parser"))


# --------------------------------------------------------------------------- subpages

# How deep container nesting may go. The FE Manual reaches depth 2 (manual → Appendix
# III → its content); the cap is a guard against a link cycle the visited-set misses.
MAX_SUBPAGE_DEPTH = 4

# The subpage template wraps real content in this container; everything outside it is
# site navigation, which would otherwise dominate a page's text and churn its hash on
# unrelated site-wide edits.
SUBPAGE_CONTENT_SELECTORS = ("div.border-box", "main")

# Number-column headers that name nothing ("Sr. No." tells you the rows are numbered,
# not what they are), so the part noun has to come from the table's caption instead.
_GENERIC_KEY_HEADERS = {"sr no", "s no", "sr", "no", "number", "#", "serial no", "item", "items"}
_PLURALS = {
    "appendices": "Appendix", "annexures": "Annexure", "annexes": "Annex",
    "chapters": "Chapter", "schedules": "Schedule", "parts": "Part",
    "sections": "Section", "forms": "Form", "guides": "Guide", "documents": "Document",
}
# A part key: "12", "III", "A-I", "IV(a)". Deliberately narrow so a title never
# masquerades as a key.
_PART_KEY_RE = re.compile(r"^[0-9]{1,3}[a-z]?$|^[ivxlc]+$|^[a-z]{1,3}-[0-9ivxlc]{1,6}$", re.IGNORECASE)


def _subpage_content_root(soup: BeautifulSoup):
    for selector in SUBPAGE_CONTENT_SELECTORS:
        node = soup.select_one(selector)
        if node is not None:
            return node
    return soup.find("body") or soup


def page_slug(url: str | None) -> str | None:
    """The `<slug>` of a /laws-regulations/<slug> URL — a child's identity namespace."""
    if not url:
        return None
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1] or None


def child_identity(parent_slug: str, part_key: str) -> str:
    """The stable id of one part of a container document.

    Keyed on the part number rather than the title: "Chapter 13" stays Chapter 13 across
    editions while its subject line gets re-worded. Parts that have no number fall back
    to their title, which is then the only stable thing about them.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sbp-law:{parent_slug}:{part_key}"))


def _part_links(node) -> list[str]:
    """Document and subpage links inside a node, in order, deduplicated."""
    urls: list[str] = []
    for anchor in node.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        if route_link(href) in (ROUTE_PDF, ROUTE_SUBPAGE) and href not in urls:
            urls.append(href)
    return urls


def _singularize(word: str) -> str | None:
    """"CHAPTERS" / "Chapter No." -> "Chapter"; "Appendices" -> "Appendix".

    Returns None for headers that name nothing, so the caller can look elsewhere.
    """
    cleaned = _tidy(word).replace(".", " ").casefold()
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" :-")
    cleaned = re.sub(r"\s*(?:no|number)$", "", cleaned).strip()
    if not cleaned or cleaned in _GENERIC_KEY_HEADERS:
        return None
    if cleaned in _PLURALS:
        return _PLURALS[cleaned]
    if len(cleaned) > 3 and cleaned.endswith("s") and not cleaned.endswith("ss"):
        cleaned = cleaned[:-1]
    return cleaned.title()


# A row whose subject line opens with one of these names itself, e.g. "Annexure - I
# Central Bank Survey", overrides the column header — SBP files its annexures in a
# column headed "Chapter No.".
_TITLE_NOUN_RE = re.compile(
    r"^(appendix|annexure|annex|chapter|schedule|part|section|form)\b", re.IGNORECASE
)


def _title_noun(title: str) -> str | None:
    match = _TITLE_NOUN_RE.match(_tidy(title))
    return match.group(1).title() if match else None


def _table_caption(table) -> str | None:
    """The nearest text above a table — "Appendices" for the FE Manual's second table."""
    node = table
    for _ in range(8):
        node = node.find_previous(string=True)
        if node is None:
            return None
        text = _tidy(str(node))
        if len(text) > 2:
            return text
    return None


def _header_cells(table) -> list[str]:
    header = table.find("tr")
    if header is None:
        return []
    return [_tidy(cell.get_text(" ")) for cell in header.find_all(["td", "th"])]


def _parse_table_parts(table, base_url: str, start_order: int) -> list[dict]:
    """Parse one subpage table into parts.

    Layouts differ per page — the FE Manual numbers its chapters, CPIS has no number
    column at all — so the key column is found from the shape of the body cells rather
    than from header wording, and rows carrying no document link are skipped (headers,
    and the prose tables inside an HTML-content page).
    """
    rows = table.find_all("tr")
    if not rows:
        return []

    headers = _header_cells(table)
    body: list[tuple[list[str], list[str]]] = []
    for row in rows:
        cells = [_tidy(cell.get_text(" ")) for cell in row.find_all(["td", "th"])]
        links = _part_links(row)
        if links:
            body.append((cells, links))
    if not body:
        return []

    # The key column is the one whose body cells are all short keys ("12", "III", "-").
    key_index: int | None = None
    for index in range(max(len(cells) for cells, _ in body)):
        values = [cells[index] for cells, _ in body if index < len(cells)]
        if not values:
            continue
        if all(_PART_KEY_RE.match(value) or value in {"-", ""} for value in values) and any(
            _PART_KEY_RE.match(value) for value in values
        ):
            key_index = index
            break

    noun = None
    if key_index is not None:
        if key_index < len(headers):
            noun = _singularize(headers[key_index])
        noun = noun or _singularize(_table_caption(table) or "")

    parts: list[dict] = []
    for offset, (cells, links) in enumerate(body):
        key_value = cells[key_index] if key_index is not None and key_index < len(cells) else ""
        # Longest remaining cell is the subject line; on a one-column table that is the
        # key column itself only when there is no key column at all.
        candidates = [
            (index, value) for index, value in enumerate(cells)
            if index != key_index and value
        ]
        title = max(candidates, key=lambda item: len(item[1]))[1] if candidates else key_value
        if not title:
            continue

        if key_value and key_value not in {"-", ""} and _PART_KEY_RE.match(key_value):
            part_key = key_value.upper()
            row_noun = _title_noun(title) or noun
            label = f"{row_noun} {part_key}" if row_noun else part_key
        else:
            part_key = normalize_law_title(title)
            label = title
        parts.append({
            "part_key": part_key,
            "part_label": label,
            "title": title,
            "url": urljoin(base_url, links[0]),
            "order": start_order + offset,
        })
    return parts


def _parse_card_parts(root, base_url: str, start_order: int) -> list[dict]:
    """Parse the card layout, where the real title is the card heading.

    Every card's link says "Download Document", so anchor text is useless here.
    """
    parts: list[dict] = []
    for offset, card in enumerate(root.select("div.category-box")):
        links = _part_links(card)
        heading = card.select_one("h5, h4, h3, .title")
        title = _tidy(heading.get_text(" ")) if heading else ""
        if not links or not title:
            continue
        parts.append({
            "part_key": normalize_law_title(title),
            "part_label": title,
            "title": title,
            "url": urljoin(base_url, links[0]),
            "order": start_order + offset,
        })
    return parts


def _parse_inline_parts(root, base_url: str, start_order: int) -> list[dict]:
    """Parse document links sitting in prose, using the link text as the title."""
    parts: list[dict] = []
    for offset, anchor in enumerate(root.select("a[href]")):
        href = (anchor.get("href") or "").strip()
        if not href or route_link(href) not in (ROUTE_PDF, ROUTE_SUBPAGE):
            continue
        title = _tidy(anchor.get_text(" ")) or _tidy(unquote(Path(urlparse(href).path).stem))
        parts.append({
            "part_key": normalize_law_title(title),
            "part_label": title,
            "title": title,
            "url": urljoin(base_url, href),
            "order": start_order + offset,
        })
    return parts


def parse_subpage(html: bytes, base_url: str) -> dict:
    """Parse a /laws-regulations/<slug> page into {parts, content_text}.

    A subpage is either a container (its parts are separate documents that revise
    independently) or a document in its own right whose content is the page itself —
    Appendix III of the FE Manual is ~36 notifications of inline HTML with no files.
    No parts found means the latter.
    """
    soup = BeautifulSoup(html, "html.parser")
    root = _subpage_content_root(soup)

    parts: list[dict] = []
    for table in root.find_all("table"):
        parts.extend(_parse_table_parts(table, base_url, len(parts)))
    parts.extend(_parse_card_parts(root, base_url, len(parts)))
    if not parts:
        parts = _parse_inline_parts(root, base_url, 0)

    # One URL, one part: tables and prose links overlap on some pages.
    unique: dict[str, dict] = {}
    for part in parts:
        unique.setdefault(part["url"], part)

    return {
        "parts": list(unique.values()),
        "content_text": extract_sbp_text(str(root).encode()),
    }


# --------------------------------------------------------------------------- archive


def _archive_name(content_hash: str, url: str) -> str:
    # Child files carry percent-encoded names ("CPIS-Forms-I_%281%29.XLS"); archive them
    # under the name SBP shows, not the escaped one.
    filename = unquote(Path(urlparse(url).path).name) or "document"
    return f"{content_hash[:8]}-{filename}"


def download_law_file(
    document_id: str, url: str, force: bool = False
) -> tuple[Path | None, str | None, str | None]:
    """Download a document file and archive it under its content hash.

    Returns (path, content_hash, error). The hash is only knowable after the bytes
    arrive, so every sync re-downloads; what the hash saves is the re-extraction and a
    duplicate archive copy. An existing archive file is never overwritten — that copy is
    the historical record, and SBP does not keep another one.
    """
    file_type = Path(urlparse(url).path).suffix.lower().lstrip(".") or None
    response = None
    temp_path = LAWS_ARCHIVE_DIR / document_id / f".part-{uuid.uuid4().hex}"
    temp_path.parent.mkdir(parents=True, exist_ok=True)

    def abandon(error: str) -> tuple[None, None, str]:
        """Drop the partial download, leaving no archive directory for a document we
        never captured. SBP serves plenty of dead links as 200-with-HTML."""
        temp_path.unlink(missing_ok=True)
        try:
            temp_path.parent.rmdir()
        except OSError:
            pass
        return None, None, error

    try:
        response = _get_sbp(url, headers=HEADERS, timeout=60, stream=True)
        response.raise_for_status()
        digest = hashlib.sha256()
        with temp_path.open("wb") as output:
            for index, chunk in enumerate(response.iter_content(chunk_size=1024 * 1024)):
                if not chunk:
                    continue
                if index == 0 and not _content_matches_file_type(chunk, file_type):
                    return abandon(f"{url} did not return a valid {file_type} file.")
                digest.update(chunk)
                output.write(chunk)

        content_hash = digest.hexdigest()
        destination = temp_path.parent / _archive_name(content_hash, url)
        if destination.exists() and not force:
            temp_path.unlink(missing_ok=True)
        else:
            temp_path.replace(destination)
        return destination, content_hash, None
    except Exception as exc:
        logging.warning("Failed to download law document %s: %s", url, exc)
        return abandon(str(exc))
    finally:
        if response is not None:
            response.close()


# --------------------------------------------------------------------------- sync


def upsert_document(db: Session, row: dict, now: datetime) -> RegDocument:
    """Create or refresh the `RegDocument` a listing row resolves to.

    Two live rows can resolve to one document (parallel editions); the later row wins
    the display title, which is why currency is decided separately, never here.
    """
    document = db.query(RegDocument).filter(RegDocument.id == row["id"]).first()
    if document is None:
        document = RegDocument(id=row["id"], first_seen_at=now)
        db.add(document)

    document.title = row["title"]
    document.normalized_title = row["normalized_title"]
    document.doc_type = row["doc_type"]
    document.source_url = row["url"]
    document.is_external = 1 if row["route"] == ROUTE_EXTERNAL else 0
    document.listed_date = row["listed_date"] or document.listed_date
    document.last_seen_at = now
    # A row that reappears after being dropped from the listing is live again.
    document.delisted_at = None
    return document


def sync_document_version(
    db: Session,
    document: RegDocument,
    row: dict,
    now: datetime,
    force: bool = False,
    verbose: bool = False,
) -> tuple[RegDocumentVersion | None, bool, str | None]:
    """Capture the content behind one listing row as a version.

    Returns (version, created, error). A hash already on file means SBP has not changed
    the document since we last looked: the existing version is touched, not replaced.
    """
    local_path, content_hash, error = download_law_file(
        document.id, row["url"], force=force
    )
    if local_path is None:
        return None, False, error

    existing = (
        db.query(RegDocumentVersion)
        .filter(
            RegDocumentVersion.document_id == document.id,
            RegDocumentVersion.content_hash == content_hash,
        )
        .first()
    )
    if existing is not None and not force:
        existing.last_seen_at = now
        existing.version_label = row["version_label"]
        existing.effective_from = row["effective_from"]
        if verbose:
            print(f"    [LAW] Unchanged: {row['title'][:60]}")
        return existing, False, None

    version = existing or RegDocumentVersion(
        id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"sbp-law-version:{document.id}:{content_hash}")),
        document_id=document.id,
        content_hash=content_hash,
        first_seen_at=now,
        # Nothing is in force until `select_current_versions` says so. If a sync dies
        # mid-pass the previously current version stays current, which is safer than a
        # freshly fetched row promoting itself outside the rule.
        is_current=0,
    )
    version.file_url = row["url"]
    version.local_path = str(local_path.relative_to(PROJECT_ROOT))
    version.file_type = local_path.suffix.lstrip(".").lower() or None
    version.version_label = row["version_label"]
    version.effective_from = row["effective_from"]
    version.last_seen_at = now
    version.source = "live"
    version.is_vectorized = 0

    text, status, extraction_error = extract_document_text(local_path, version.file_type)
    version.content_text = text
    version.extraction_status = status
    version.extraction_error = extraction_error
    if existing is None:
        db.add(version)
    if verbose:
        print(f"    [LAW] New version ({status}): {row['title'][:60]}")
    return version, existing is None, None


def link_document_to_circular(
    db: Session,
    document: RegDocument,
    circular,
    link_type: str = "listing",
    detected_via: str = "listing",
    confidence: float | None = None,
) -> RegDocumentLink:
    """Record a circular ↔ document edge exactly once."""
    link = (
        db.query(RegDocumentLink)
        .filter(
            RegDocumentLink.circular_id == circular.id,
            RegDocumentLink.document_id == document.id,
            RegDocumentLink.link_type == link_type,
        )
        .first()
    )
    if link is None:
        link = RegDocumentLink(
            circular_id=circular.id,
            document_id=document.id,
            link_type=link_type,
            detected_via=detected_via,
            confidence=confidence,
        )
        db.add(link)
        db.flush()
    return link


def resolve_circular_row(
    db: Session, document: RegDocument, verbose: bool = False
) -> bool:
    """Point a listing row at the circular it actually is, instead of copying it.

    Several Guidelines in the listing are circulars SBPEye already holds in full. Storing
    their text again would fork one document into two records that drift apart, so the
    RegDocument keeps no content and carries `circular_id` plus a link edge.

    Returns False when the circular is not in the database yet — the row stays a stub and
    the next sync retries it, since circular sync may simply not have reached it.
    """
    circular = find_circular_by_url(db, document.source_url)
    if circular is None:
        if verbose:
            print(f"    [LAW] Circular not indexed yet: {document.title[:55]}")
        return False

    document.circular_id = circular.id
    link_document_to_circular(db, document, circular)
    if verbose:
        print(f"    [LAW] Resolved to {circular.display_name}: {document.title[:45]}")
    return True


def upsert_child(
    db: Session, parent: RegDocument, part: dict, now: datetime
) -> RegDocument:
    """Create or refresh one part of a container document."""
    parent_slug = parent.page_slug or page_slug(parent.source_url) or parent.id
    child_id = child_identity(parent_slug, part["part_key"])
    child = db.query(RegDocument).filter(RegDocument.id == child_id).first()
    if child is None:
        child = RegDocument(id=child_id, first_seen_at=now)
        db.add(child)

    child.title = part["title"]
    child.normalized_title = normalize_law_title(part["title"])
    # A part is the same kind of thing as the document it belongs to.
    child.doc_type = parent.doc_type
    child.source_url = part["url"]
    child.parent_id = parent.id
    child.part_label = part["part_label"]
    child.part_order = part["order"]
    child.is_external = 1 if route_link(part["url"]) == ROUTE_EXTERNAL else 0
    child.last_seen_at = now
    child.delisted_at = None
    return child


def sync_html_version(
    db: Session,
    document: RegDocument,
    content_text: str,
    now: datetime,
    verbose: bool = False,
) -> tuple[RegDocumentVersion, bool]:
    """Capture a page whose content *is* the page, with no file behind it.

    The hash is taken over the cleaned text rather than the raw HTML, so a site-wide
    template tweak does not read as a new edition of the law.
    """
    content_hash = hashlib.sha256(content_text.encode("utf-8")).hexdigest()
    existing = (
        db.query(RegDocumentVersion)
        .filter(
            RegDocumentVersion.document_id == document.id,
            RegDocumentVersion.content_hash == content_hash,
        )
        .first()
    )
    if existing is not None:
        existing.last_seen_at = now
        return existing, False

    version = RegDocumentVersion(
        id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"sbp-law-version:{document.id}:{content_hash}")),
        document_id=document.id,
        content_hash=content_hash,
        file_url=None,
        local_path=None,
        file_type="html",
        content_text=content_text,
        extraction_status="extracted",
        first_seen_at=now,
        last_seen_at=now,
        source="live",
        is_current=0,
    )
    db.add(version)
    # Flush so the currency pass, which queries versions back, can see this one.
    db.flush()
    if verbose:
        print(f"    [LAW] New HTML version: {document.title[:60]}")
    return version, True


def write_manifest_version(
    db: Session,
    container: RegDocument,
    parts: list[dict],
    child_hashes: dict[str, str | None],
    now: datetime,
    verbose: bool = False,
) -> tuple[RegDocumentVersion | None, bool]:
    """Record what a container held, as a version hashed over its children's hashes.

    A container has no bytes of its own — the FE Manual *is* its 22 chapters, which
    revise independently. Hashing the child hashes means a new manifest row appears
    exactly when some part actually changed, which makes the manifest history double as
    the change log: diff two manifests to see which chapters moved between two syncs.
    """
    manifest = {
        "document_id": container.id,
        "parts": [
            {
                "id": part["child_id"],
                "part_key": part["part_key"],
                "part_label": part["part_label"],
                "title": part["title"],
                "content_hash": child_hashes.get(part["child_id"]),
            }
            for part in sorted(parts, key=lambda item: item["order"])
        ],
    }
    digest_basis = "\n".join(
        f"{entry['id']}:{entry['content_hash'] or ''}" for entry in manifest["parts"]
    )
    content_hash = hashlib.sha256(digest_basis.encode("utf-8")).hexdigest()

    existing = (
        db.query(RegDocumentVersion)
        .filter(
            RegDocumentVersion.document_id == container.id,
            RegDocumentVersion.content_hash == content_hash,
        )
        .first()
    )
    if existing is not None:
        existing.last_seen_at = now
        return existing, False

    version = RegDocumentVersion(
        id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"sbp-law-version:{container.id}:{content_hash}")),
        document_id=container.id,
        content_hash=content_hash,
        file_url=None,
        local_path=None,
        # Not "html": a manifest is bookkeeping, not readable text, and phase 5 must keep
        # it out of the search index.
        file_type="manifest",
        content_text=json.dumps(manifest, indent=2),
        extraction_status="extracted",
        first_seen_at=now,
        last_seen_at=now,
        source="live",
        is_current=0,
    )
    db.add(version)
    db.flush()
    if verbose:
        print(f"    [LAW] New manifest ({len(manifest['parts'])} parts): {container.title[:50]}")
    return version, True


def delist_missing_children(
    db: Session, parent_id: str, seen_ids: set[str], now: datetime
) -> int:
    """Mark parts that disappeared from their container's page. Nothing is deleted."""
    query = db.query(RegDocument).filter(
        RegDocument.parent_id == parent_id,
        RegDocument.delisted_at.is_(None),
    )
    if seen_ids:
        query = query.filter(RegDocument.id.notin_(seen_ids))
    missing = query.all()
    for document in missing:
        document.delisted_at = now
    return len(missing)


def _observe(observed: dict[str, list[dict]], document_id: str, version, order: int = 0) -> None:
    """Note that this pass pointed at `version` — the currency tiebreak reads this.

    Without it, two versions first seen in the same sync are indistinguishable and the
    superseded one can keep the crown.
    """
    observed.setdefault(document_id, []).append(
        {"version_id": version.id, "order": order, "listed_date": None}
    )


def sync_subpage(
    db: Session,
    document: RegDocument,
    now: datetime,
    counts: dict,
    touched: set[str],
    visited: set[str],
    observed: dict[str, list[dict]] | None = None,
    force: bool = False,
    delay: float = 0.0,
    verbose: bool = False,
    depth: int = 0,
) -> str | None:
    """Sync a /laws-regulations/<slug> document, recursing into nested containers.

    Returns the document's own content hash so a parent can fold it into its manifest.
    """
    import time

    observed = {} if observed is None else observed
    url = document.source_url
    if not url or url in visited or depth > MAX_SUBPAGE_DEPTH:
        current = document.current_version
        return current.content_hash if current else None
    visited.add(url)
    document.page_slug = page_slug(url)

    parsed = parse_subpage(fetch_page_cached(url, force=force), url)
    parts = parsed["parts"]

    if not parts:
        version, created = sync_html_version(
            db, document, parsed["content_text"], now, verbose=verbose
        )
        touched.add(document.id)
        _observe(observed, document.id, version)
        counts["new_versions" if created else "unchanged"] += 1
        return version.content_hash

    if verbose:
        print(f"    [LAW] Container with {len(parts)} part(s): {document.title[:50]}")

    child_hashes: dict[str, str | None] = {}
    seen_children: set[str] = set()
    for part in parts:
        child = upsert_child(db, document, part, now)
        db.flush()
        part["child_id"] = child.id
        touched.add(child.id)
        seen_children.add(child.id)
        counts["children"] += 1

        route = route_link(part["url"])
        if route == ROUTE_SUBPAGE:
            child_hashes[child.id] = sync_subpage(
                db, child, now, counts, touched, visited, observed,
                force=force, delay=delay, verbose=verbose, depth=depth + 1,
            )
        elif route == ROUTE_PDF:
            row = {
                "title": part["title"],
                "url": part["url"],
                "version_label": parse_version_label(part["title"]),
                "effective_from": parse_effective_from(part["title"]),
            }
            try:
                version, created, error = sync_document_version(
                    db, child, row, now, force=force, verbose=verbose
                )
            except Exception as exc:  # one bad part must not lose the whole container
                version, created, error = None, False, str(exc)
            if version is None:
                counts["errors"] += 1
                print(f"  [ERROR] {part['url']}: {error}")
                current = child.current_version
                child_hashes[child.id] = current.content_hash if current else None
            else:
                db.flush()
                child_hashes[child.id] = version.content_hash
                _observe(observed, child.id, version, part["order"])
                counts["new_versions" if created else "unchanged"] += 1
            if delay:
                time.sleep(delay)
        elif route == ROUTE_CIRCULAR:
            # A part can be a circular too, and gets the same treatment as a listing row.
            if resolve_circular_row(db, child, verbose=verbose):
                counts["resolved"] += 1
            else:
                counts["stubs"] += 1
        else:
            counts["stubs"] += 1

        db.commit()

    counts["delisted"] += delist_missing_children(db, document.id, seen_children, now)
    version, created = write_manifest_version(
        db, document, parts, child_hashes, now, verbose=verbose
    )
    touched.add(document.id)
    if version is not None:
        _observe(observed, document.id, version)
    if created:
        counts["manifests"] += 1
    db.commit()
    return version.content_hash if version else None


# ------------------------------------------------------------------------- indexing


def law_document(document: RegDocument, version: RegDocumentVersion) -> dict:
    """The text document fed to the chunker for one law/regulation version."""
    label = document.title or ""
    if document.part_label and document.parent is not None:
        label = f"{document.parent.title} - {document.part_label}: {label}"
    return {
        "doc_id": version.id,
        "doc_type": "law",
        "doc_label": label,
        "text": version.content_text or "",
        "file_type": version.file_type or "",
    }


def law_chunk_metadata(
    document: RegDocument, version: RegDocumentVersion, chunk: dict, index: int
) -> dict:
    """Chroma metadata for one chunk of a law/regulation.

    `kind` is the corpus discriminator the law vector arm filters on. `doc_type` keeps
    its existing collection-wide meaning — which kind of thing the chunk came from — so
    the law's own law/regulation/guideline classification rides along as `law_type`.
    """
    return {
        "kind": "law",
        "doc_type": "law",
        "law_type": document.doc_type or "",
        "document_id": document.id,
        "version_id": version.id,
        "title": document.title or "",
        "part_label": document.part_label or "",
        "url": version.file_url or document.source_url or "",
        "chunk_index": index,
        "ref": chunk["ref"],
        "unit_id": chunk["unit_id"],
        "source_start": chunk["source_start"],
        "source_end": chunk["source_end"],
        **({"page_start": chunk["page_start"]} if chunk.get("page_start") else {}),
        **({"page_end": chunk["page_end"]} if chunk.get("page_end") else {}),
    }


def vectorize_law_document(
    db: Session, document: RegDocument, verbose: bool = False
) -> int:
    """Replace a document's Chroma chunks with its in-force text. Returns chunk count.

    Superseded versions are dropped from the vector store — their text is still in
    SQLite and archived on disk, but a semantic hit on wording SBP no longer publishes
    would be worse than no hit at all.
    """
    version = document.current_version
    searchable = (
        version is not None
        and version.file_type not in NON_TEXT_LAW_FILE_TYPES
        and (version.content_text or "").strip()
    )
    if not searchable:
        _replace_document_chunks(
            {"doc_id": document.id, "doc_type": "law", "doc_label": "", "text": "",
             "file_type": ""},
            metadata_for=lambda chunk, i: {},
            delete_kwargs={"law_document_id": document.id},
        )
        for stale in document.versions:
            stale.is_vectorized = 0
        return 0

    count = _replace_document_chunks(
        law_document(document, version),
        metadata_for=lambda chunk, i: law_chunk_metadata(document, version, chunk, i),
        delete_kwargs={"law_document_id": document.id},
    )
    # Marked done even at zero chunks: a scanned PDF that extracts to nothing has been
    # processed, and leaving it unmarked would re-chunk it on every sync forever.
    for other in document.versions:
        other.is_vectorized = 1 if other.id == version.id else 0
    if verbose:
        print(f"  [CHROMA] Indexed law: {document.title[:50]} ({count} chunks)")
    return count


def index_law_document(db: Session, document: RegDocument, verbose: bool = False) -> None:
    """Refresh both search indexes for one document.

    Paired deliberately: FTS and Chroma writes travel together, the same rule circulars
    follow, so a document can never be lexically findable but semantically invisible.
    """
    try:
        vectorize_law_document(db, document, verbose=verbose)
    except Exception:
        logging.exception("ChromaDB indexing failed for law document %s", document.id)
    index_law_fts(db, document)


def index_pending_laws(
    db: Session,
    document_ids: set[str] | None = None,
    force: bool = False,
    verbose: bool = False,
) -> int:
    """Index documents whose in-force text is not in the search indexes yet.

    `is_vectorized` on the current version is the ledger: a new capture or a currency
    flip leaves it 0, so only documents that actually changed pay the embedding cost.
    Documents with nothing readable in force (containers, failed downloads) are skipped —
    a manifest is bookkeeping, not text.
    """
    query = db.query(RegDocument)
    if document_ids is not None:
        if not document_ids:
            return 0
        query = query.filter(RegDocument.id.in_(document_ids))

    indexed = 0
    for document in query.all():
        version = document.current_version
        if version is None or version.file_type in NON_TEXT_LAW_FILE_TYPES:
            continue
        if not (version.content_text or "").strip():
            continue
        if not force and version.is_vectorized == 1:
            continue
        index_law_document(db, document, verbose=verbose)
        indexed += 1
    db.commit()
    return indexed


def select_current_versions(
    db: Session,
    document_ids: set[str],
    rows_by_document: dict[str, list[dict]] | None = None,
    now: datetime | None = None,
) -> None:
    """Decide which version of each document is in force, once per document.

    Fetch order must never decide this: the listing can carry an in-force edition and a
    future-dated one at the same time, and processing them row by row would flip
    `is_current` back and forth. The rule (§3 of the plan):

      * a version whose `effective_from` is still in the future is pending, never current;
      * otherwise the latest arrived `effective_from` wins;
      * with no effective dates to compare, the version this listing pass actually
        pointed at wins (latest listing row), falling back to the most recently captured.

    Because a pending version becomes current merely by its date arriving, this runs on
    every sync, not only when a new hash shows up.
    """
    now = now or datetime.utcnow()
    rows_by_document = rows_by_document or {}
    for document_id in document_ids:
        versions = (
            db.query(RegDocumentVersion)
            .filter(
                RegDocumentVersion.document_id == document_id,
                RegDocumentVersion.source == "live",
            )
            .all()
        )
        if not versions:
            continue

        observed = {
            row["version_id"]: row
            for row in rows_by_document.get(document_id, [])
            if row.get("version_id")
        }
        eligible = [
            v for v in versions if v.effective_from is None or v.effective_from <= now
        ]
        dated = [v for v in eligible if v.effective_from is not None]

        if dated:
            winner = max(dated, key=lambda v: v.effective_from)
        elif eligible:
            winner = max(
                eligible,
                key=lambda v: (
                    v.id in observed,
                    (observed.get(v.id, {}).get("listed_date") or datetime.min),
                    observed.get(v.id, {}).get("order", -1),
                    v.first_seen_at or datetime.min,
                ),
            )
        else:
            # Everything is future-dated: nothing is in force yet.
            winner = None

        for version in versions:
            version.is_current = 1 if version is winner else 0
    # Flush so callers reading currency back with a query see this pass, not the state
    # it replaced — sessions here run with autoflush off.
    db.flush()


def delist_missing(db: Session, seen_ids: set[str], now: datetime) -> int:
    """Mark documents that vanished from the listing, keeping every row and file.

    Only safe after a complete pass — a filtered or truncated sync has not seen the
    whole listing and would delist documents it simply never looked at.
    """
    query = db.query(RegDocument).filter(
        RegDocument.delisted_at.is_(None),
        # Children come from subpages (phase 3), not from the listing, so their absence
        # here says nothing about whether they are still published.
        RegDocument.parent_id.is_(None),
    )
    if seen_ids:
        query = query.filter(RegDocument.id.notin_(seen_ids))
    missing = query.all()
    for document in missing:
        document.delisted_at = now
    return len(missing)


def sync_laws(
    db: Session,
    doc_types: list[str] | None = None,
    limit: int = 0,
    force: bool = False,
    delay: float = 0.5,
    verbose: bool = False,
    skip_subpages: bool = False,
    skip_indexing: bool = False,
) -> dict:
    """Sync the laws & regulations listing into `reg_documents`.

    Directly-linked files become versions; subpage rows are followed into their parts
    (recursively, since a part can be a container itself) and summarised by a manifest
    version. Circular-typed rows stay stubs until phase 4, and external rows are
    metadata-only by design.
    """
    import time

    now = datetime.utcnow()
    rows = fetch_listing(force=force)
    total_rows = len(rows)
    if doc_types:
        wanted = set(doc_types)
        rows = [row for row in rows if row["doc_type"] in wanted]
    full_pass = not doc_types and (limit <= 0 or limit >= len(rows))
    if limit > 0:
        rows = rows[:limit]

    if verbose:
        print(f"Listing has {total_rows} row(s); processing {len(rows)}")

    seen_ids: set[str] = set()
    touched: set[str] = set()
    visited_subpages: set[str] = set()
    rows_by_document: dict[str, list[dict]] = {}
    counts = {
        "rows": len(rows),
        "documents": 0,
        "children": 0,
        "manifests": 0,
        "new_versions": 0,
        "unchanged": 0,
        "resolved": 0,
        "stubs": 0,
        "external": 0,
        "delisted": 0,
        "indexed": 0,
        "errors": 0,
    }

    for index, row in enumerate(rows, start=1):
        document = upsert_document(db, row, now)
        if document.id not in seen_ids:
            seen_ids.add(document.id)
            touched.add(document.id)
            counts["documents"] += 1
        rows_by_document.setdefault(document.id, []).append(row)

        if row["route"] == ROUTE_SUBPAGE and not skip_subpages:
            try:
                sync_subpage(
                    db, document, now, counts, touched, visited_subpages,
                    rows_by_document, force=force, delay=delay, verbose=verbose,
                )
            except Exception as exc:  # a broken container must not end the pass
                counts["errors"] += 1
                print(f"  [ERROR] {row['url']}: {exc}")
        elif row["route"] == ROUTE_PDF:
            try:
                version, created, error = sync_document_version(
                    db, document, row, now, force=force, verbose=verbose
                )
            except Exception as exc:  # one bad document must not end the pass
                version, created, error = None, False, str(exc)
            if version is None:
                counts["errors"] += 1
                print(f"  [ERROR] {row['url']}: {error}")
            else:
                db.flush()
                row["version_id"] = version.id
                counts["new_versions" if created else "unchanged"] += 1
            if delay:
                time.sleep(delay)
        elif row["route"] == ROUTE_CIRCULAR:
            if resolve_circular_row(db, document, verbose=verbose):
                counts["resolved"] += 1
            else:
                counts["stubs"] += 1
        elif row["route"] == ROUTE_EXTERNAL:
            counts["external"] += 1
            if verbose:
                print(f"    [LAW] External, metadata only: {row['title'][:60]}")
        else:
            counts["stubs"] += 1
            if verbose:
                print(f"    [LAW] Stub ({row['route']}): {row['title'][:60]}")

        db.commit()
        if verbose:
            print(f"[{index}/{len(rows)}] {row['title'][:70]}")

    # Children and containers are decided here too — their currency has no listing row
    # behind it, so it falls through to "the version we most recently captured".
    select_current_versions(db, touched, rows_by_document, now=now)
    if full_pass:
        counts["delisted"] += delist_missing(db, seen_ids, now)
    db.commit()

    # Indexing runs after currency is settled: what gets indexed is the text in force,
    # which is not knowable until every row of the pass has been seen.
    if not skip_indexing:
        counts["indexed"] = index_pending_laws(db, touched, force=force, verbose=verbose)

    print(
        f"\nLaws sync complete. Documents: {counts['documents']} "
        f"(+{counts['children']} part(s)), new versions: {counts['new_versions']}, "
        f"manifests: {counts['manifests']}, unchanged: {counts['unchanged']}, "
        f"circulars: {counts['resolved']}, stubs: {counts['stubs']}, "
        f"external: {counts['external']}, indexed: {counts['indexed']}, "
        f"delisted: {counts['delisted']}, errors: {counts['errors']}"
    )
    return counts
