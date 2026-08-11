"""Deterministic cross-linking between circulars and laws/regulations.

Phase 6a of docs/LAWS_REGULATIONS_PLAN.md — no LLM involved. Every edge here comes from
one of two hard signals:

* a **URL** in a circular's text (or one of its attachments) that resolves to a document
  or version we already hold, and
* a document's own **name** appearing in a circular's text, including "Chapter 12 of the
  FE Manual" style references to one part of a container.

Both say the circular *mentions* the regulation, which is why they are recorded as
`references`. Deciding that a circular *amends* a regulation is a judgement about meaning
and is left to the AI pass (phase 6b) — writing "amends" here would put a claim in the
database that nothing actually checked.

The module deliberately imports only models and URL helpers, so both the laws scraper and
the circular scraper can depend on it without an import cycle.
"""

import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from sqlalchemy.orm import Session

from .models import Attachment, Circular, RegDocument, RegDocumentLink, RegDocumentVersion

# Path fragments that mark a URL as *possibly* pointing into the laws corpus. They are a
# cheap pre-filter only: `/assets/document/` is a store circular annexures share, so a hit
# still has to resolve against a known document URL to become a link.
LAWS_URL_HINTS = ("laws_regulations", "laws-regulations", "/assets/document/")

_URL_RE = re.compile(r"""https?://[^\s"'<>()\[\]]+""", re.IGNORECASE)

# A document name shorter than this is not identifying: "Reporting Guidelines" would match
# any circular that happens to discuss reporting guidelines in general.
MIN_TITLE_WORDS = 3

# How far from a container's name we will look for "Chapter 12".
PART_REFERENCE_WINDOW = 200

_PART_NOUNS = "chapter|appendix|annexure|annex|schedule|part|section"
_PART_REFERENCE_RE = re.compile(
    rf"\b(?:{_PART_NOUNS})s?\.?\s*(?:no\.?\s*)?([0-9]{{1,3}}|[ivxlc]{{1,6}})\b",
    re.IGNORECASE,
)


def _canon(text: str | None) -> str:
    """Reduce text to lowercase alphanumeric words, so punctuation cannot block a match.

    "Credit Bureau Rules, 2016" and "Credit Bureau Rules 2016" are the same name.
    """
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def normalize_link(url: str | None) -> str:
    """Canonical form of a URL for equality comparison.

    Strips the fragment and query, plus sentence punctuation glued to the end — SBP's own
    circulars write "...see https://www.sbp.org.pk/laws-regulations/foreign-exchange-manual."
    """
    if not url:
        return ""
    cleaned = url.split("#")[0].split("?")[0]
    cleaned = cleaned.rstrip(".,;:!)\"'")
    return cleaned.lower().rstrip("/")


def identifying_names(title: str) -> set[str]:
    """The names specific enough to identify this document in running text.

    A short name is a phrase, not an identifier: linking every circular that says
    "reporting guidelines" to the document of that name would be noise. The one
    abbreviation worth generating is the initialism of a three-word title — SBP cites the
    Foreign Exchange Manual as "FE Manual" far more often than in full. Longer titles are
    not abbreviated this way, and generating one anyway invents strings nobody writes.
    """
    canonical = _canon(title)
    words = canonical.split()
    names: set[str] = set()
    if len(words) >= MIN_TITLE_WORDS:
        names.add(canonical)
    if len(words) == 3:
        initialism = f"{words[0][0]}{words[1][0]} {words[2]}"
        if len(initialism) >= 8:
            names.add(initialism)
    return names


# --------------------------------------------------------------------------- indexes


def build_url_index(db: Session) -> dict[str, str]:
    """Every URL we know a document by → its document id.

    Version file URLs win over listing URLs: a link to the PDF is a link to that document,
    while a container's page URL is the coarser fallback.
    """
    index: dict[str, str] = {}
    for document in db.query(RegDocument).filter(RegDocument.source_url.isnot(None)):
        key = normalize_link(document.source_url)
        if key:
            index.setdefault(key, document.id)
    for version in db.query(RegDocumentVersion).filter(
        RegDocumentVersion.file_url.isnot(None)
    ):
        key = normalize_link(version.file_url)
        if key:
            index[key] = version.document_id
    # The bare listing page identifies no document.
    index.pop(normalize_link("https://www.sbp.org.pk/laws-regulations"), None)
    return index


def build_name_index(db: Session) -> list[tuple[str, str]]:
    """(name, document_id) pairs for top-level documents, longest name first.

    Only top-level documents are matched by name. A part's own title is its subject line —
    "EXPORTS", "Authorized Dealers" — which would match half the corpus; parts are reached
    through their container instead, see `build_part_index`.

    Externally hosted laws are included. We hold no text for the Banking Companies
    Ordinance, but it is a first-class row in the corpus and the most-cited statute in it;
    leaving it out would mean a law that appears in `/api/laws` and in no circular's link
    list. `is_external` already tells a consumer the text lives off-site.
    """
    names: list[tuple[str, str]] = []
    for document in db.query(RegDocument).filter(RegDocument.parent_id.is_(None)):
        for name in identifying_names(document.normalized_title or document.title):
            names.append((name, document.id))
    names.sort(key=lambda item: -len(item[0]))
    return names


def build_part_index(db: Session) -> dict[str, dict[str, str]]:
    """{container name: {part key: child document id}} for containers with numbered parts.

    Keyed on the part number rather than its title, which is exactly how circulars cite
    them: "in terms of Chapter 12 of the Foreign Exchange Manual".
    """
    index: dict[str, dict[str, str]] = {}
    containers = db.query(RegDocument).filter(RegDocument.children.any()).all()
    for container in containers:
        parts: dict[str, str] = {}
        for child in container.children:
            if not child.part_label:
                continue
            match = re.search(r"([0-9]{1,3}|[ivxlc]{1,6})\s*$", child.part_label.strip(), re.I)
            if match:
                parts[match.group(1).lower()] = child.id
        if not parts:
            continue
        for name in identifying_names(container.normalized_title or container.title):
            index[name] = parts
    return index


# --------------------------------------------------------------------------- matching


def find_law_urls(text: str | None) -> set[str]:
    """Normalized URLs in `text` that could point into the laws corpus."""
    found = set()
    for raw in _URL_RE.findall(text or ""):
        normalized = normalize_link(raw)
        if any(hint in normalized for hint in LAWS_URL_HINTS):
            found.add(normalized)
    return found


def match_by_url(text: str | None, url_index: dict[str, str]) -> set[str]:
    return {
        url_index[url] for url in find_law_urls(text) if url in url_index
    }


def match_by_name(
    text: str | None,
    name_index: list[tuple[str, str]],
    part_index: dict[str, dict[str, str]] | None = None,
) -> set[str]:
    """Documents named in `text`, resolving part references to the part itself.

    When a circular cites "Chapter 12 of the FE Manual" the link points at Chapter 12, not
    at the manual: the chapter is the document that would change.
    """
    canonical = _canon(text)
    if not canonical:
        return set()

    matched: set[str] = set()
    part_index = part_index or {}
    for name, document_id in name_index:
        start = canonical.find(name)
        if start < 0:
            continue

        parts = part_index.get(name)
        if parts:
            resolved = _resolve_parts(canonical, name, parts)
            if resolved:
                matched |= resolved
                continue
        matched.add(document_id)
    return matched


def _resolve_parts(canonical: str, name: str, parts: dict[str, str]) -> set[str]:
    """Part ids cited near any mention of the container's name."""
    resolved: set[str] = set()
    for mention in re.finditer(re.escape(name), canonical):
        window = canonical[
            max(0, mention.start() - PART_REFERENCE_WINDOW):
            mention.end() + PART_REFERENCE_WINDOW
        ]
        for key in _PART_REFERENCE_RE.findall(window):
            child_id = parts.get(key.lower())
            if child_id:
                resolved.add(child_id)
    return resolved


# ------------------------------------------------------------------------ link writing


def link_document_to_circular(
    db: Session,
    document: RegDocument,
    circular: Circular,
    link_type: str = "listing",
    detected_via: str = "listing",
    confidence: float | None = None,
) -> RegDocumentLink:
    """Record a circular ↔ document edge exactly once, per link type."""
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


def link_circular_to_laws(
    db: Session,
    circular: Circular,
    url_index: dict[str, str] | None = None,
    name_index: list[tuple[str, str]] | None = None,
    part_index: dict[str, dict[str, str]] | None = None,
    request_refetch: bool = False,
    verbose: bool = False,
) -> dict[str, int]:
    """Link one circular to every law/regulation it points at or names.

    Pass the prebuilt indexes when looping over many circulars; they are rebuilt per call
    otherwise, which is fine for the scrape-time hook on a single circular.

    `request_refetch` flags the linked documents for the next `laws sync` to look at. It is
    for *newly scraped* circulars — a circular that just arrived citing a regulation is the
    signal that a new edition of that regulation may exist. The historical backfill leaves
    it alone: flagging 75 documents because 3,600 old circulars mention them is just a
    full re-sync with extra steps.
    """
    url_index = build_url_index(db) if url_index is None else url_index
    name_index = build_name_index(db) if name_index is None else name_index
    part_index = build_part_index(db) if part_index is None else part_index

    text = circular.content_text or ""
    by_url = match_by_url(text, url_index)
    for attachment in circular.attachments:
        key = normalize_link(attachment.original_url)
        if key in url_index:
            by_url.add(url_index[key])
    by_name = match_by_name(text, name_index, part_index) - by_url

    counts = {"url_scan": 0, "name_match": 0}
    for document_id, detected_via in (
        [(did, "url_scan") for did in by_url] + [(did, "name_match") for did in by_name]
    ):
        document = db.query(RegDocument).filter(RegDocument.id == document_id).first()
        if document is None:
            continue
        link_document_to_circular(
            db, document, circular, link_type="references", detected_via=detected_via
        )
        if request_refetch:
            document.refetch_requested = 1
        counts[detected_via] += 1
        if verbose:
            print(f"    [LINK] {circular.display_name} -> {document.title[:50]} ({detected_via})")
    return counts


def backlink_circulars(
    db: Session,
    limit: int = 0,
    rescan: bool = False,
    request_refetch: bool = False,
    verbose: bool = False,
) -> dict[str, int]:
    """Scan the circular corpus for references to laws & regulations.

    Idempotent: links are keyed on (circular, document, type), so re-running adds only
    what is genuinely new.
    """
    url_index = build_url_index(db)
    name_index = build_name_index(db)
    part_index = build_part_index(db)

    query = db.query(Circular)
    if not rescan:
        # Circulars that already carry a scanned link are skipped unless asked otherwise.
        linked = {row[0] for row in db.query(RegDocumentLink.circular_id).filter(
            RegDocumentLink.detected_via.in_(("url_scan", "name_match"))
        ).distinct()}
        if linked:
            query = query.filter(Circular.id.notin_(linked))
    if limit > 0:
        query = query.limit(limit)

    totals = {"scanned": 0, "linked": 0, "url_scan": 0, "name_match": 0}
    for circular in query.all():
        counts = link_circular_to_laws(
            db, circular, url_index, name_index, part_index,
            request_refetch=request_refetch, verbose=verbose,
        )
        totals["scanned"] += 1
        totals["url_scan"] += counts["url_scan"]
        totals["name_match"] += counts["name_match"]
        if counts["url_scan"] or counts["name_match"]:
            totals["linked"] += 1
        if totals["scanned"] % 200 == 0:
            db.commit()
    db.commit()
    return totals
