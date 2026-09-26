"""Checks run on a finished chat answer — `docs/CHAT_REDESIGN.md` R6, warn-only.

Until now nothing looked at an answer after the model wrote it. The prompt asks the model
to cite only what it read and to say when a rule has been replaced, and the system took
its word. Two of the design's five checks are built here, because both are lookups — no
model call — against records the turn already keeps:

**Grounding (check 2).** Every cited document's *text* reached the model this turn. A
citation resolves whenever its handle appeared anywhere in the model's context, and much
of what appears there is a pointer, not a reading: an `amended_by` entry, a
`references_laws` line, a withdrawn match, an annexure listing, an attachment manifest, a
citation replayed from an earlier answer. Cited from one of those, a document reaches the
reader as a working link that looks like a verified source. The turn's ledgers — which
C1a, C12, C5 and C1 built to stop text going out twice — say exactly which documents'
text went out once, so this is set membership.

**Supersession (check 4).** Nothing cited is withdrawn, or changed by a later `amends`
edge, without the answer saying so. C11 tells the model which circulars were replaced
and amended; nothing checked that the answer passed it on. A superseded circular quoted
as current is the most damaging answer a regulatory tool can give.

Both only *warn*: the answer is never altered. They are measured on a benchmark round
before either is allowed to withhold anything — the model legitimately names an amender
it was told about without reading it ("this was later amended by …"), which is exactly
what the evidence card's lineage line invites.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from typing import Any

from sqlalchemy.orm import Session, selectinload

from .citation_handles import TOKEN_PATTERN
from .models import Attachment, Circular, CircularRelationship

# Tools whose result rows *are* the documents they list — a value with its context, a
# circular in a recency or tag listing, an inventory row with its excerpt. Citing one of
# those rows is citing what the tool showed. Every other tool is accounted by the
# ledgers, because its payload also names documents it did not deliver (a circular's
# amenders, its attachment manifest).
LISTING_TOOLS = frozenset({
    "query_regulatory_values",
    "get_latest_circulars",
    "get_circulars_by_tag",
    "search_regulatory_inventory",
})

_WITHDRAWN = frozenset({"superseded", "cancelled"})
_WITHDRAWAL_WORDS = re.compile(
    r"\b(supersed\w*|cancel\w*|withdrawn|replaced|rescind\w*|repeal\w*|no longer in force)\b",
    re.IGNORECASE,
)
MAX_RELATED = 3


def cited_documents(answer: str) -> list[tuple[str, str, str, str]]:
    """``(kind, id, label, token)`` for each distinct document the answer cites, in order."""
    seen: set[tuple[str, str]] = set()
    cited = []
    for match in TOKEN_PATTERN.finditer(answer or ""):
        kind, identifier = match.group(1), match.group(2).strip()
        if (kind, identifier) in seen:
            continue
        seen.add((kind, identifier))
        cited.append((kind, identifier, (match.group(3) or "").strip(), match.group(0)))
    return cited


def check_answer(
    answer: str,
    db: Session,
    *,
    sent_text_keys: Mapping[str, Collection[str]],
    sent_passages: Mapping[str, Collection[str]],
    listed_documents: Collection[tuple[str, str]],
    selected_circular_ids: Collection[str] = (),
) -> list[dict[str, Any]]:
    """The warnings for one finished answer, most severe first. Empty when it passes."""
    cited = cited_documents(answer)
    if not cited:
        return []
    read = set(sent_text_keys) | {owner for owner, keys in sent_passages.items() if keys}
    read |= set(selected_circular_ids)
    warnings = _supersession(answer, db, cited) + _grounding(
        db, cited, sent_text_keys, sent_passages, listed_documents, selected_circular_ids,
        _lineage(db, cited, read),
    )
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(warnings, key=lambda item: order[item["severity"]])


# ------------------------------------------------------------------------ grounding


def _lineage(
    db: Session, cited: list[tuple[str, str, str, str]], read: set[str],
) -> set[str]:
    """Cited circulars related by an edge to a circular the turn read, or another cited.

    "The 2025 PRs have since been amended by X" is a fact from the relationship graph,
    which C11 put in front of the model on purpose — as the `amended_by` annotation on the
    circular it *read*. Naming X that way does not require having read X, and an answer
    that does it and says it could not see X's provisions, as the 2026-08-26 P12 answer
    did, is doing exactly the right thing. Replaying both benchmark rounds, both grounding
    warnings were this; in P12 the circular whose annotation named X was read and not
    cited, which is why the edge is taken against the read set too.
    """
    ids = {identifier for kind, identifier, _, _ in cited if kind == "circular"}
    known = ids | read
    if not ids or len(known) < 2:
        return set()
    edges = db.query(CircularRelationship).filter(
        CircularRelationship.source_id.in_(list(known)),
        CircularRelationship.target_id.in_(list(known)),
    ).all()
    related = {edge.source_id for edge in edges} | {edge.target_id for edge in edges}
    return related & ids


def _grounding(
    db: Session,
    cited: list[tuple[str, str, str, str]],
    sent_text_keys: Mapping[str, Collection[str]],
    sent_passages: Mapping[str, Collection[str]],
    listed: Collection[tuple[str, str]],
    selected: Collection[str],
    lineage: set[str] = frozenset(),
) -> list[dict[str, Any]]:
    warnings = []
    attachment_ids = [identifier for kind, identifier, _, _ in cited if kind == "attachment"]
    owners = {
        item.id: item.circular_id
        for item in db.query(Attachment).filter(Attachment.id.in_(attachment_ids))
    } if attachment_ids else {}

    for kind, identifier, _label, token in cited:
        if (kind, identifier) in listed or (kind == "circular" and identifier in lineage):
            continue
        if kind in ("circular", "law"):
            grounded = (
                identifier in sent_text_keys
                or bool(sent_passages.get(identifier))
                or (kind == "circular" and identifier in selected)
            )
        else:
            owner = owners.get(identifier)
            prefix = f"{identifier}__"
            grounded = owner is not None and (
                owner in selected
                or any(key.startswith(prefix) for key in sent_passages.get(owner, ()))
            )
        if grounded:
            continue
        warnings.append({
            "check": "grounding",
            "severity": "low",
            "citation": token,
            "message": (
                "Cited, but none of its text was read in this answer's research — it "
                "appeared only as a reference (an amendment, a linked law, an annexure "
                "listing, or an earlier answer). Check it before relying on what the "
                "answer attributes to it."
            ),
        })
    return warnings


# ---------------------------------------------------------------------- supersession


def _reference_key(text: str) -> str:
    """A reference reduced to what identifies it, for finding it in prose.

    SBP's own references are not written consistently, and an answer writes them the way
    a person does: "SH&SFD Circular No.04 of 2026 of 2026" (no space, the duplicated year —
    AGENTS.md, known issues) is "SH&SFD Circular No. 04 of 2026" in the answer, and
    "IH & SMEFD" is "IH&SMEFD". Case, spacing, punctuation, leading zeros and a repeated
    trailing year go; the letters and numbers that identify the circular stay. Found in the
    2026-09-27 round, where P06 named its replacing circular in the first sentence and
    raised three high warnings for not naming it.
    """
    folded = re.sub(r"\b(of\s+(\d{4}))\s+of\s+\2\b", r"\1", (text or "").casefold())
    tokens = re.findall(r"[a-z]+|\d+", folded)
    return "".join(str(int(token)) if token.isdigit() else token for token in tokens)


def _mentions(answer: str, circular: Circular, cited_ids: set[str]) -> bool:
    """Whether the answer names `circular` — by citation, or by its reference in prose."""
    if circular.id in cited_ids:
        return True
    reference = _reference_key(circular.reference or "")
    return bool(reference) and reference in _reference_key(answer)


def _paragraph_of(answer: str, token: str) -> str:
    for paragraph in re.split(r"\n\s*\n", answer):
        if token in paragraph:
            return paragraph
    return ""


def _citation(circular: Circular) -> str:
    return f"[[circular:{circular.id}|{circular.display_name}]]"


def _supersession(
    answer: str, db: Session, cited: list[tuple[str, str, str, str]],
) -> list[dict[str, Any]]:
    tokens = {identifier: token for kind, identifier, _, token in cited if kind == "circular"}
    if not tokens:
        return []
    circulars = (
        db.query(Circular)
        .options(selectinload(Circular.amended_by).selectinload(CircularRelationship.source))
        .filter(Circular.id.in_(list(tokens)))
        .all()
    )
    cited_ids = set(tokens)
    warnings = []
    for circular in circulars:
        edges = [edge for edge in circular.amended_by or [] if edge.source is not None]
        edges.sort(key=lambda edge: (edge.source.date is None, edge.source.date), reverse=True)
        replacing = [edge.source for edge in edges if edge.type in ("supersedes", "cancels")]
        # An amendment that has itself been withdrawn changes nothing any more: 2026-09-27
        # P20 was told to name FE Circular No. 02 of 2023, which EPD CL 07/2025 cancelled.
        amending = [
            edge.source for edge in edges
            if edge.type == "amends" and (edge.source.status or "active") not in _WITHDRAWN
        ]
        token = tokens[circular.id]
        withdrawn = (circular.status or "active") in _WITHDRAWN or bool(replacing)

        if withdrawn:
            if any(_mentions(answer, item, cited_ids) for item in replacing):
                continue
            if _WITHDRAWAL_WORDS.search(_paragraph_of(answer, token)):
                continue
            warnings.append({
                "check": "supersession",
                "severity": "high",
                "citation": token,
                "message": (
                    f"This circular is {circular.status or 'no longer in force'}, and the "
                    "answer cites it without saying so or naming what replaced it."
                ),
                "related": [_citation(item) for item in replacing[:MAX_RELATED]],
            })
        elif (
            amending
            and not any(_mentions(answer, item, cited_ids) for item in amending)
            # Cited as the *earlier* rule ("… set by X and since superseded by the 2025
            # PRs"): the answer is not presenting X's text as current, so what amended
            # X does not change what it says. 2026-09-26 P12.
            and not _WITHDRAWAL_WORDS.search(_paragraph_of(answer, token))
        ):
            warnings.append({
                "check": "supersession",
                "severity": "medium",
                "citation": token,
                "message": (
                    "A later circular amends this one, and the answer does not mention it. "
                    "A figure or requirement quoted from it may have changed."
                ),
                "related": [_citation(item) for item in amending[:MAX_RELATED]],
            })
    return warnings
