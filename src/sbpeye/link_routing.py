from pathlib import Path
import re
from urllib.parse import unquote, urldefrag, urlencode, urlparse

from bs4 import BeautifulSoup, NavigableString
from sqlalchemy import extract, func, or_
from sqlalchemy.orm import Session

from .models import Attachment, Circular
from .circular_identity import (
    CIRCULAR_REFERENCE_RE, REFERENCE_DATE_YEAR_RE, CircularReference,
    _normalize_prefix, _nearby_reference_year, _reference_parts, _grouped_numbers,
    normalize_reference, iter_circular_references, infer_reference_year,
)


DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx"}

from .sbp_urls import is_allowed_sbp_url, normalize_sbp_url


def attachment_info(url: str) -> dict:
    path = Path(urlparse(url).path)
    extension = path.suffix.lower()
    if extension not in DOCUMENT_EXTENSIONS:
        raise ValueError("This SBP link is not a supported document.")
    return {"url": url, "filename": path.name or f"document{extension}", "file_type": extension.lstrip(".")}


def _candidate_year(circular: Circular) -> int | None:
    if circular.date:
        return circular.date.year
    for value in (circular.reference, circular.url):
        if not value:
            continue
        match = re.search(r"\b((?:19|20)\d{2})\b", value)
        if match:
            return int(match.group(1))
    return None


def _resolve_circular_reference_from_parts(
    parts: dict,
    current: Circular,
    db: Session,
) -> Circular | None:
    # The DB may store "BC & CPD" where the normalized prefix is "BC&CPD"; let the
    # SQL prefilter match either spacing — the exact parts comparison below keeps precision.
    prefix_pattern = parts["prefix"].replace("&", "%&%")
    query = db.query(Circular).filter(
        or_(
            Circular.reference.ilike(f"{prefix_pattern}%Circular%"),
            Circular.title.ilike(f"{prefix_pattern}%Circular%"),
        )
    )
    if parts["year"]:
        query = query.filter(
            or_(
                extract("year", Circular.date) == parts["year"],
                Circular.reference.ilike(f"%{parts['year']}%"),
                Circular.url.ilike(f"%/{parts['year']}/%"),
            )
        )

    matches: list[Circular] = []
    for candidate in query.all():
        if candidate.id == current.id:
            continue
        candidate_parts = _reference_parts(candidate.reference) or _reference_parts(candidate.title)
        if not candidate_parts:
            continue
        if (
            candidate_parts["prefix"] != parts["prefix"]
            or candidate_parts["is_letter"] != parts["is_letter"]
            or candidate_parts["number"] != parts["number"]
        ):
            continue
        candidate_year = candidate_parts["year"] or _candidate_year(candidate)
        if parts["year"] and candidate_year != parts["year"]:
            continue
        matches.append(candidate)
    if len(matches) == 1:
        return matches[0]
    return None


def resolve_reference_parts(parts: dict, current: Circular, db: Session) -> Circular | None:
    """Resolve an already-parsed reference (e.g. a CircularReference from
    ``iter_circular_references``) to a stored circular. Public entry point for callers
    that harvest references in bulk, such as annexure withdrawal lists."""
    return _resolve_circular_reference_from_parts(parts, current, db)


def _resolve_circular_reference(
    reference_text: str,
    current: Circular,
    db: Session,
    inferred_year: int | None = None,
) -> Circular | None:
    parts = _reference_parts(reference_text, inferred_year)
    if not parts:
        return None
    return _resolve_circular_reference_from_parts(parts, current, db)


def resolve_reference_in_context(
    db: Session,
    current: Circular,
    reference_text: str,
) -> Circular | None:
    """Resolve a free-text circular reference, inferring the year from `current`'s content.

    Mirrors inline-link resolution: a bare reference like "DMMD Circular no. 20" is
    disambiguated by the year of the nearby "dated ..." text in the source content, so
    relationship targets resolve to the same circular the inline link points to.
    """
    parts = _reference_parts(reference_text)
    if not parts:
        return None
    if parts["year"] is None:
        # Prefer a year written into the reference itself ("... dated May 08, 2003",
        # "... of October 8, 2008"), then fall back to inferring it from where the
        # reference is mentioned in the source content.
        embedded = re.search(r"\b(?:19|20)\d{2}\b", reference_text)
        year = int(embedded.group()) if embedded else infer_reference_year(
            current.content_text, parts["prefix"], parts["is_letter"], parts["number"]
        )
        parts = {**parts, "year": year}
    return _resolve_circular_reference_from_parts(parts, current, db)


def _link_plain_circular_references(soup: BeautifulSoup, circular: Circular, db: Session) -> None:
    resolved: dict[tuple, Circular | None] = {}
    for text_node in list(soup.find_all(string=CIRCULAR_REFERENCE_RE)):
        parent = text_node.parent
        if parent and parent.name in {"a", "script", "style", "textarea"}:
            continue

        text = str(text_node)
        replacements = []
        last_end = 0
        for reference in iter_circular_references(text):
            key = (reference.prefix, reference.is_letter, reference.number, reference.year)
            if key not in resolved:
                resolved[key] = _resolve_circular_reference_from_parts(
                    {
                        "prefix": reference.prefix,
                        "is_letter": reference.is_letter,
                        "number": reference.number,
                        "year": reference.year,
                    },
                    circular,
                    db,
                )
            target = resolved[key]
            if not target:
                continue

            if reference.label_start > last_end:
                replacements.append(NavigableString(text[last_end:reference.label_start]))
            anchor = soup.new_tag("a", href=f"/circulars/{target.id}")
            anchor["class"] = "document-pill circular-reference-pill"
            anchor["data-document-link"] = "true"
            anchor["data-document-kind"] = "circular"
            anchor["title"] = target.title
            anchor.string = text[reference.label_start:reference.label_end]
            replacements.append(anchor)
            last_end = reference.label_end

        if not replacements:
            continue
        if last_end < len(text):
            replacements.append(NavigableString(text[last_end:]))
        text_node.replace_with(*replacements)


def find_circular_by_url(db: Session, url: str | None) -> Circular | None:
    """The circular published at `url`, or None.

    A circular is reachable at up to three URLs after the July-2026 redesign — its new
    slug, its original path, and whatever `url` mirrors — so all three are candidates.
    Comparison is case-insensitive because SBP's own pages link the same circular with
    inconsistent casing.
    """
    if not url:
        return None
    try:
        normalized = normalize_sbp_url(url)
    except ValueError:
        return None
    lowered = normalized.lower()
    return (
        db.query(Circular)
        .filter(
            or_(
                func.lower(Circular.url) == lowered,
                func.lower(Circular.new_url) == lowered,
                func.lower(Circular.old_url) == lowered,
            )
        )
        .first()
    )


def harvest_reference_links(html: str | bytes, db: Session, current: Circular) -> list[Circular]:
    """Circulars that ``current``'s detail page hyperlinks to.

    The redesigned site pre-renders in-text references as real ``<a>`` links to other
    circular slugs. Resolving those anchors against stored circulars gives deterministic
    relationship targets (no LLM needed), which callers merge with the model's output.
    """
    soup = BeautifulSoup(html, "html.parser")
    targets: dict[str, Circular] = {}
    for anchor in soup.find_all("a", href=True):
        try:
            url = normalize_sbp_url(anchor.get("href", ""))
        except ValueError:
            continue
        path = urlparse(url).path
        # Only individual circular detail slugs, not the paginated listing (…/circulars/P30).
        if "/circulars/" not in path or re.search(r"/circulars/P\d+$", path):
            continue
        match = find_circular_by_url(db, url)
        if match is not None and match.id != current.id:
            targets[match.id] = match
    return list(targets.values())


def _find_attachment_id(url: str, circular: Circular, db: Session) -> str | None:
    """The stored attachment `url` points at, matching by URL and then by filename.

    An exact `original_url` match is the common case. It misses whenever the href
    written into the circular's HTML is not the URL the attachment was downloaded
    from, which `detect_attachments` deliberately allows: bare relative filenames
    (`href="C3-Annex.pdf"`) are resolved against the flat asset store rather than the
    circular's own pretty URL, and pre-redesign absolute paths (`/dmmd/2023/C13-Annex-A.pdf`)
    are stored under whichever location actually served the file. Without a fallback
    those annexures render as plain links back to a dead sbp.org.pk path instead of
    document pills.

    The filename fallback is scoped to `circular`'s own attachments, so a generic
    name like `annexure.pdf` can never resolve to another circular's enclosure.
    """
    exact = db.query(Attachment.id).filter(
        func.lower(Attachment.original_url) == url.lower()
    ).first()
    if exact:
        return exact[0]

    filename = unquote(Path(urlparse(url).path).name)
    if not filename:
        return None
    by_name = db.query(Attachment.id).filter(
        Attachment.circular_id == circular.id,
        func.lower(Attachment.filename) == filename.lower(),
    ).first()
    return by_name[0] if by_name else None


def rewrite_document_links(html: str, circular: Circular, db: Session) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        try:
            url = normalize_sbp_url(anchor.get("href", ""))
        except ValueError:
            continue
        known = db.query(Circular.id).filter(func.lower(Circular.url) == url.lower()).first()
        attachment = _find_attachment_id(url, circular, db)
        if known:
            target, kind = f"/circulars/{known[0]}", "circular"
        elif attachment:
            target = f"/documents/open?{urlencode({'id': attachment})}"
            kind = Path(urlparse(url).path).suffix.lstrip(".").upper() or "document"
        else:
            continue
        anchor["href"] = target
        anchor["class"] = list(dict.fromkeys([*(anchor.get("class") or []), "document-pill"]))
        anchor["data-document-link"] = "true"
        anchor["data-document-kind"] = kind
        anchor.attrs.pop("target", None)
        anchor.attrs.pop("rel", None)
    _link_plain_circular_references(soup, circular, db)
    return str(soup)
