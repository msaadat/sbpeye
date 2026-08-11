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
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from ..database import PROJECT_ROOT
from ..link_routing import DOCUMENT_EXTENSIONS, is_allowed_sbp_url
from ..models import RegDocument, RegDocumentVersion
from .circulars import (
    ATTACHMENTS_DIR,
    BASE_URL,
    HEADERS,
    _content_matches_file_type,
    _get_sbp,
    extract_document_text,
    fetch_page_cached,
)

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


# --------------------------------------------------------------------------- archive


def _archive_name(content_hash: str, url: str) -> str:
    filename = Path(urlparse(url).path).name or "document"
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
    try:
        response = _get_sbp(url, headers=HEADERS, timeout=60, stream=True)
        response.raise_for_status()
        digest = hashlib.sha256()
        with temp_path.open("wb") as output:
            for index, chunk in enumerate(response.iter_content(chunk_size=1024 * 1024)):
                if not chunk:
                    continue
                if index == 0 and not _content_matches_file_type(chunk, file_type):
                    temp_path.unlink(missing_ok=True)
                    return None, None, f"{url} did not return a valid {file_type} file."
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
        temp_path.unlink(missing_ok=True)
        # Leave no empty archive directory behind for a document we never captured.
        try:
            temp_path.parent.rmdir()
        except OSError:
            pass
        logging.warning("Failed to download law document %s: %s", url, exc)
        return None, None, str(exc)
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
) -> dict:
    """Sync the laws & regulations listing into `reg_documents`.

    Phase 2 fetches content for directly-linked document files. Subpage and circular rows
    are recorded as stubs — they are real documents with real identities, just without
    content until phases 3 and 4 — and external rows are metadata-only by design.
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
    rows_by_document: dict[str, list[dict]] = {}
    counts = {
        "rows": len(rows),
        "documents": 0,
        "new_versions": 0,
        "unchanged": 0,
        "stubs": 0,
        "external": 0,
        "errors": 0,
    }

    for index, row in enumerate(rows, start=1):
        document = upsert_document(db, row, now)
        if document.id not in seen_ids:
            seen_ids.add(document.id)
            counts["documents"] += 1
        rows_by_document.setdefault(document.id, []).append(row)

        if row["route"] == ROUTE_PDF:
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

    select_current_versions(db, seen_ids, rows_by_document, now=now)
    counts["delisted"] = delist_missing(db, seen_ids, now) if full_pass else 0
    db.commit()

    print(
        f"\nLaws sync complete. Documents: {counts['documents']}, "
        f"new versions: {counts['new_versions']}, unchanged: {counts['unchanged']}, "
        f"stubs: {counts['stubs']}, external: {counts['external']}, "
        f"delisted: {counts['delisted']}, errors: {counts['errors']}"
    )
    return counts
