"""Deterministic digests of the tool calls behind one chat answer.

The chat view lists the research steps a turn took, but until now a step was only a
label: what the tool actually returned lived for the length of the request and was
gone. This module turns a tool's JSON response into a small, renderable record —
which documents it found, and the passage that made each one a match — so a reader
can open a step afterwards and see the evidence the answer was built on.

Nothing here asks a model anything. Every tool response has a known shape, built a
few hundred lines away in `AIClient._execute_tool`, so the digest is a parse rather
than a summary: the same call on the same payload always produces the same record.

Two size rules, because these are persisted per assistant message rather than held
for the length of a request. A step keeps at most `_MAX_HITS` documents and reports
how many it dropped, and each passage is clipped to `_SNIPPET_CHARS` — enough to
recognise why a document matched, which is all a step needs to be worth opening.
Anyone who wants the untruncated payload is looking for the trace inspector, which
records it in full and is admin-only for that reason.

Digests are built *before* `CitationHandles.to_handles` rewrites the payload, so the
citation tokens here are the real ones the frontend already knows how to resolve.
"""

from __future__ import annotations

import json
from typing import Any

# Bumped when the shape below changes in a way a stored row cannot be read as. The
# reader tolerates an unknown version by refusing to render rather than guessing.
STEP_SCHEMA_VERSION = 1

_MAX_HITS = 20
_SNIPPET_CHARS = 280
_ARGUMENT_CHARS = 200
# Ranked arms of one `search_corpus` response, in the order the tool builds them.
# A document can place in several; it is listed once, with every arm that found it.
_SEARCH_ARMS = (
    ("reference_matches", "reference"),
    ("lexical_results", "keyword"),
    ("semantic_results", "meaning"),
    ("results", "match"),
)


def _clip(value: Any, limit: int) -> str | None:
    """One line of `value`, clipped to `limit` characters with an ellipsis."""
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _hit(**fields: Any) -> dict:
    """A hit with its empty fields dropped, so the view can test for presence."""
    return {key: value for key, value in fields.items() if value not in (None, "", [])}


def _snippet(row: dict) -> str | None:
    """The best passage a result row carries, in descending order of directness.

    A located chunk beats a term-density window, which beats the circular's own
    summary — the last of which is not evidence of a match at all, and is used only
    because a row with no text at all reads as a document that could not be opened.
    """
    passages = row.get("matching_passages")
    if isinstance(passages, list) and passages:
        first = passages[0]
        if isinstance(first, dict) and first.get("passage"):
            return _clip(first["passage"], _SNIPPET_CHARS)
    for key in ("matching_passage_excerpt", "passage", "context", "summary"):
        text = _clip(row.get(key), _SNIPPET_CHARS)
        if text:
            return text
    return None


def _note(row: dict) -> str | None:
    """What the row says about itself beyond the match: repeats, amendment, withdrawal."""
    parts: list[str] = []
    if row.get("duplicate_of_earlier_entry"):
        parts.append("Text shown in an earlier step")
    for key, prefix in (
        ("amended_by", "Amended by"),
        ("replaced_by", "Replaced by"),
        ("superseded_by", "Superseded by"),
    ):
        value = row.get(key)
        if isinstance(value, list) and value:
            parts.append(prefix + " " + ", ".join(str(item) for item in value[:2]))
        elif isinstance(value, str) and value:
            parts.append(prefix + " " + value)
    return _clip(" · ".join(parts), _SNIPPET_CHARS) if parts else None


def _document_hit(row: dict, *, match: list[str] | None = None) -> dict:
    """Map any of the circular-shaped result rows onto the common hit shape."""
    status = row.get("status")
    title = row.get("title") or row.get("resolved_title") or row.get("source_label")
    return _hit(
        citation=row.get("citation") or row.get("attachment_citation"),
        title=_clip(title, 200) or "Untitled",
        reference=_clip(row.get("reference"), 120),
        date=row.get("date") or row.get("effective_date"),
        department=_clip(row.get("department") or row.get("law_type"), 120),
        # "active" is the default and says nothing; anything else changes how the
        # hit should be read, which is the only reason the field is carried.
        status=status if status and status != "active" else None,
        snippet=_snippet(row),
        note=_note(row),
        match=match,
    )


def _rows(payload: dict, key: str) -> list[dict]:
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _search_hits(payload: dict) -> list[dict]:
    """Fold the arms of a search response into one list, each document listed once."""
    ordered: list[dict] = []
    by_citation: dict[str, dict] = {}
    for key, label in _SEARCH_ARMS:
        for row in _rows(payload, key):
            citation = row.get("citation")
            existing = by_citation.get(citation) if citation else None
            if existing is not None:
                # A later arm found a document an earlier one already described. It is
                # the same document, so it becomes a label on the existing hit rather
                # than a duplicate row the reader has to work out is one.
                existing.setdefault("match", []).append(label)
                continue
            hit = _document_hit(row, match=[label])
            if citation:
                by_citation[citation] = hit
            ordered.append(hit)
    for row in _rows(payload, "law_results"):
        ordered.append(_document_hit(row, match=["law"]))
    for row in _rows(payload, "withdrawn_matches"):
        ordered.append(_document_hit(row, match=["withdrawn"]))
    return ordered


def _value_label(row: dict) -> str | None:
    """A regulatory value written the way the row states it."""
    if row.get("value_text"):
        return _clip(row["value_text"], 120)
    number = row.get("value")
    if number is None:
        return None
    parts = [str(row.get("comparator") or "").strip(), str(number)]
    if row.get("value_high") is not None:
        parts.append("– " + str(row["value_high"]))
    if row.get("unit"):
        parts.append(str(row["unit"]))
    return _clip(" ".join(part for part in parts if part), 120)


def _value_hits(payload: dict) -> list[dict]:
    hits = []
    for row in _rows(payload, "results"):
        status = row.get("circular_status")
        hits.append(_hit(
            citation=row.get("citation"),
            title=_clip(row.get("metric"), 200) or "Value",
            reference=_clip(row.get("subject"), 120),
            date=row.get("effective_date") or row.get("circular_date"),
            department=_clip(row.get("document"), 120),
            status=status if status and status != "active" else None,
            snippet=_clip(row.get("context"), _SNIPPET_CHARS),
            note=_value_label(row),
        ))
    return hits


def _law_hits(payload: dict) -> list[dict]:
    """One hit per passage returned from inside a single law."""
    citation = payload.get("citation")
    title = _clip(payload.get("resolved_title") or payload.get("requested"), 200) or "Law"
    passages = _rows(payload, "passages")
    if not passages:
        return [_document_hit(payload)]
    return [
        _hit(
            citation=citation,
            title=title,
            reference=_clip(row.get("locator"), 120),
            department=_clip(payload.get("law_type"), 120),
            snippet=_clip(row.get("passage"), _SNIPPET_CHARS),
            note=("Page " + str(row["page"])) if row.get("page") is not None else None,
        )
        for row in passages
    ]


def _attachment_hits(payload: dict) -> list[dict]:
    """One hit per passage read from inside a single attachment."""
    citation = payload.get("attachment_citation") or payload.get("citation")
    title = _clip(payload.get("filename"), 200) or "Attachment"
    passages = _rows(payload, "passages")
    if not passages:
        return [_document_hit({**payload, "title": title, "citation": citation})]
    return [
        _hit(
            citation=citation,
            title=title,
            reference=_clip(payload.get("circular"), 120),
            snippet=_clip(row.get("passage"), _SNIPPET_CHARS),
            note=("Page " + str(row["page"])) if row.get("page") is not None else None,
        )
        for row in passages
    ]


def _inventory_hits(payload: dict) -> list[dict]:
    hits = []
    for row in _rows(payload, "results"):
        terms = row.get("matched_terms")
        note = None
        if isinstance(terms, list) and terms:
            note = _clip("Matched: " + ", ".join(str(term) for term in terms), 160)
        hits.append(_hit(
            citation=row.get("citation") or row.get("attachment_citation"),
            title=_clip(row.get("title"), 200) or "Untitled",
            reference=_clip(row.get("reference") or row.get("locator"), 120),
            date=row.get("date"),
            department=_clip(row.get("department") or row.get("law_type"), 120),
            snippet=_clip(row.get("passage"), _SNIPPET_CHARS),
            note=note,
        ))
    return hits


def _hits_for(name: str, payload: dict) -> list[dict]:
    if name == "search_corpus":
        return _search_hits(payload)
    if name == "query_regulatory_values":
        return _value_hits(payload)
    if name == "get_law_details":
        return _law_hits(payload)
    if name == "read_attachment":
        return _attachment_hits(payload)
    if name == "search_regulatory_inventory":
        return _inventory_hits(payload)
    if name == "get_circular_details":
        # The ambiguous-reference branch answers with candidates instead of a document.
        candidates = _rows(payload, "candidates")
        if candidates:
            return [_document_hit(row) for row in candidates]
        return [_document_hit(payload)]
    # `search_selected_documents`, `get_latest_circulars`, `get_circulars_by_tag`, and
    # any tool added later that answers with a `results` list of document-shaped rows.
    return [_document_hit(row) for row in _rows(payload, "results")]


def _summary(name: str, payload: dict, hits: list[dict], omitted: int) -> str:
    """One line naming what the step found, phrased for the tool that ran."""
    if name == "get_circular_details" and not _rows(payload, "candidates"):
        return hits[0].get("title", "Circular") if hits else "Nothing found"
    if name in ("get_law_details", "read_attachment"):
        count = payload.get("passage_count")
        if isinstance(count, int) and count:
            title = (
                payload.get("resolved_title") or payload.get("filename")
                or ("the law" if name == "get_law_details" else "the attachment")
            )
            return f"{count} passage{'s' if count != 1 else ''} from {title}"
    if name == "search_regulatory_inventory":
        matched = payload.get("documents_matched")
        returned = payload.get("documents_returned", len(hits))
        if isinstance(matched, int) and isinstance(returned, int) and matched > returned:
            return f"{returned} of {matched} matching documents"
    total = len(hits) + omitted
    if not total:
        return "Nothing found"
    if name == "search_selected_documents":
        noun = "passage"
    elif name == "query_regulatory_values":
        noun = "value"
    else:
        noun = "document"
    line = f"{total} {noun}{'s' if total != 1 else ''}"
    return f"{line} ({omitted} not shown)" if omitted else line


def _compact_arguments(arguments: Any) -> dict:
    if not isinstance(arguments, dict):
        return {}
    compact: dict[str, Any] = {}
    for key, value in arguments.items():
        if value in (None, "", [], {}):
            continue
        compact[str(key)] = _clip(value, _ARGUMENT_CHARS) if isinstance(value, str) else value
    return compact


def build_step(
    name: str,
    arguments: Any,
    result: str,
    *,
    label: str,
    elapsed_ms: int | None = None,
) -> dict:
    """Digest one completed tool call into the record a research step renders from.

    `result` is the tool's own JSON string, taken before citation handles are
    substituted in, so the tokens it carries are the ones the reader can resolve.
    A payload this module cannot parse is not an error: the step still records what
    ran and with which arguments, which is the part a reader needs most.
    """
    step: dict[str, Any] = {
        "v": STEP_SCHEMA_VERSION,
        "tool": name,
        "label": label,
        "arguments": _compact_arguments(arguments),
    }
    if elapsed_ms is not None:
        step["elapsed_ms"] = elapsed_ms

    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        step["summary"] = "Result could not be read"
        return step

    # An `error` alongside `candidates` is the ambiguous-reference branch, which is a
    # result worth showing rather than a failure.
    error = payload.get("error")
    if error and not payload.get("candidates"):
        step["error"] = _clip(error, _SNIPPET_CHARS)
        step["summary"] = step["error"]
        return step

    hits = _hits_for(name, payload)
    omitted = max(0, len(hits) - _MAX_HITS)
    step["hits"] = hits[:_MAX_HITS]
    if omitted:
        step["omitted"] = omitted
    step["summary"] = _summary(name, payload, step["hits"], omitted)
    if error:
        step["error"] = _clip(error, _SNIPPET_CHARS)
    # The tool says outright when its own list was cut short, and that outranks
    # anything inferred here: it counts documents this process never saw.
    if payload.get("complete") is False or payload.get("omitted"):
        step["incomplete"] = True
    return step


def failed_step(
    name: str,
    arguments: Any,
    *,
    label: str,
    error: str,
    elapsed_ms: int | None = None,
) -> dict:
    """A step for a tool that raised instead of returning a payload."""
    step: dict[str, Any] = {
        "v": STEP_SCHEMA_VERSION,
        "tool": name,
        "label": label,
        "arguments": _compact_arguments(arguments),
        "error": _clip(error, _SNIPPET_CHARS) or "The tool failed",
    }
    step["summary"] = step["error"]
    if elapsed_ms is not None:
        step["elapsed_ms"] = elapsed_ms
    return step
