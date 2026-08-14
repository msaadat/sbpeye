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
from dataclasses import dataclass
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


def law_label(document: RegDocument) -> str:
    """How a law/regulation names itself to a reader, an index, or a model.

    A part never appears without its container: "EXPORTS" is Chapter 12 of the Foreign
    Exchange Manual or it is nothing. Lives here rather than beside any one caller because
    the chunk text, the inventory ledger and the analysis prompts must agree on what a
    document is called — three places that had grown three copies of these three lines.
    """
    label = document.title or document.id
    if document.part_label and document.parent is not None:
        return f"{document.parent.title} - {document.part_label}: {label}"
    return label


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


# ------------------------------------------------------------- law → law candidates


# Enough of the sentence for a model to tell "made under the SBP Act" from "a bank as
# defined in the Banking Companies Ordinance", which is the whole classification problem.
MENTION_WINDOW = 260
MAX_MENTION_SNIPPETS = 3

# `[[SBPEYE_PAGE:3]]` is our own bookkeeping, injected during PDF extraction — 62 of the
# 213 snippets the live corpus produces contained one, and in a prompt they read as part
# of the instrument's wording. Not anchored to a line like `checklist.PAGE_MARKER_RE`,
# because extraction is not the only thing that concatenates these.
_PAGE_MARKER_RE = re.compile(r"\[\[SBPEYE_PAGE:\d+\]\]")


def mention_snippets(
    text: str | None,
    name: str,
    limit: int = MAX_MENTION_SNIPPETS,
    window: int = MENTION_WINDOW,
) -> list[str]:
    """Readable excerpts of `text` around each mention of `name`.

    Matching runs against the original text, not the canonical form `match_by_name` uses:
    the canonical form has had punctuation and case stripped, so its offsets cannot be
    mapped back, and the evidence a classifier needs is the sentence as written.
    """
    if not text or not name:
        return []
    # Strip markers from the whole text *before* slicing, not from each excerpt after.
    # A window is cut at a character offset and will eventually land inside a marker;
    # cleaning the excerpt leaves fragments like "PEYE_PAGE:12]]" that no pattern short of
    # guessing can remove. Cleaning first makes that case impossible rather than handled.
    text = _PAGE_MARKER_RE.sub(" ", text)
    pattern = re.compile(r"\W+".join(re.escape(word) for word in name.split()), re.I)
    snippets: list[str] = []
    for match in pattern.finditer(text):
        excerpt = text[max(0, match.start() - window):match.end() + window]
        snippets.append(re.sub(r"\s+", " ", excerpt).strip())
        if len(snippets) >= limit:
            break
    return snippets


@dataclass(frozen=True)
class LawReference:
    """One law naming another, with the wording that says so."""

    target_id: str
    target_title: str
    snippets: list[str]


def find_law_references(db: Session, document: RegDocument) -> list[LawReference]:
    """Other laws named in this law's in-force text, with evidence for each.

    Deterministic: the same name index the circular backlink pass uses. What the mention
    *means* is a separate judgement, reserved for the AI pass — asserting that one Act is
    made under another because its name appears would be a claim the text may not support.

    A document's own parts, its container, and itself are excluded: those relationships
    are already modelled by `parent_id` and would be noise here.
    """
    version = document.current_version
    text = version.content_text if version is not None else None
    if not text or not text.strip():
        return []

    name_index = build_name_index(db)
    part_index = build_part_index(db)
    matched = match_by_name(text, name_index, part_index)

    excluded = {document.id, document.parent_id} | {
        child.id for child in document.children
    }
    names_by_document: dict[str, list[str]] = {}
    for name, document_id in name_index:
        names_by_document.setdefault(document_id, []).append(name)

    references: list[LawReference] = []
    for target_id in sorted(matched - {value for value in excluded if value}):
        target = db.query(RegDocument).filter(RegDocument.id == target_id).first()
        if target is None:
            continue
        # A part resolves through its container's name, so look the snippet up under
        # whichever row actually carries a matchable name.
        lookup_id = target.parent_id if target.parent_id in names_by_document else target_id
        snippets: list[str] = []
        for name in names_by_document.get(lookup_id, []):
            snippets.extend(mention_snippets(text, name))
            if len(snippets) >= MAX_MENTION_SNIPPETS:
                break
        references.append(LawReference(
            target_id=target_id,
            target_title=law_label(target),
            snippets=snippets[:MAX_MENTION_SNIPPETS],
        ))
    return references


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
