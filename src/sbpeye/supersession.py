"""Resolve blanket supersessions.

Some circulars supersede, withdraw, or consolidate *all previous instructions on a subject*
without naming any specific circular (e.g. "This will supersede all previous instructions issued
on the subject"). The LLM relationship extractor flags these via ``supersedes_all_previous`` and a
short ``subject`` phrase. This module turns that flag into concrete ``supersedes`` relationships by:

1. Shortlisting older circulars whose (normalized) title contains every significant token of the
   subject — a strict, title-similarity pre-filter.
2. Asking the LLM to confirm which shortlisted circulars genuinely concern the same subject.

Because marking a circular superseded auto-changes its status, an implausibly large match set is
treated as a signal to skip and leave for manual review rather than apply blindly.
"""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from .models import Circular, CircularRelationship
from .search import tokenize

# Confidence recorded on auto-detected blanket supersessions.
BLANKET_CONFIDENCE = 0.9
# How many shortlisted candidates to send to the LLM for confirmation.
SHORTLIST_LIMIT = 40
# If more than this many titles match the subject, it is too generic to auto-apply safely.
MAX_SHORTLIST_BEFORE_REVIEW = 80
# If the LLM confirms more than this many supersessions, skip and leave for manual review.
MAX_AUTO_APPLY = 60
# Snippet length (chars) of candidate content passed to the LLM.
SNIPPET_CHARS = 300


def _normalize(text: str | None) -> str:
    """Collapse the embedded newlines / runs of whitespace seen in scraped titles."""
    return re.sub(r"\s+", " ", text or "").strip()


def _stems(text: str | None) -> set[str]:
    """Significant tokens, crudely singularized so 'requirement(s)' compare equal."""
    return {re.sub(r"s$", "", tok) if len(tok) > 3 else tok for tok in tokenize(_normalize(text))}


def find_blanket_superseded(
    db: Session,
    client,
    current: Circular,
    subject: str,
    *,
    warn=print,
) -> list[Circular]:
    """Return the older circulars that `current`'s blanket supersession of `subject` covers.

    Returns an empty list (with a warning) when the candidate set is implausibly large, so such
    high-impact cases are left for manual review instead of being applied automatically.
    """
    subject_tokens = _stems(subject) or _stems(current.title)
    if not subject_tokens or current.date is None:
        return []

    # 1. Strict title-similarity shortlist: every significant subject token must appear in the
    #    candidate title, restricted to circulars issued before `current` (any department).
    scored: list[tuple[int, Circular]] = []
    candidates = (
        db.query(Circular)
        .filter(Circular.date.isnot(None), Circular.date < current.date, Circular.id != current.id)
        .all()
    )
    reference_tokens = subject_tokens | _stems(current.title)
    for candidate in candidates:
        title_tokens = _stems(candidate.title)
        if not subject_tokens.issubset(title_tokens):
            continue
        score = len(title_tokens & reference_tokens)
        scored.append((score, candidate))

    if len(scored) > MAX_SHORTLIST_BEFORE_REVIEW:
        warn(
            f"  [review] blanket supersession on {subject!r} matched {len(scored)} circulars "
            f"by title — too broad to auto-apply; skipping for manual review."
        )
        return []
    if not scored:
        return []

    scored.sort(key=lambda item: item[0], reverse=True)
    shortlist = [candidate for _, candidate in scored[:SHORTLIST_LIMIT]]

    # 2. LLM confirmation over the shortlist (title + small content snippet).
    payload = [
        {
            "id": candidate.id,
            "title": _normalize(candidate.title),
            "date": candidate.date.date().isoformat() if candidate.date else "",
            "snippet": _normalize(candidate.content_text)[:SNIPPET_CHARS],
        }
        for candidate in shortlist
    ]
    confirmed_ids = set(client.select_superseded(_normalize(current.title), subject, payload))
    confirmed = [candidate for candidate in shortlist if candidate.id in confirmed_ids]

    if len(confirmed) > MAX_AUTO_APPLY:
        warn(
            f"  [review] blanket supersession on {subject!r} confirmed {len(confirmed)} circulars "
            f"— above the auto-apply limit ({MAX_AUTO_APPLY}); skipping for manual review."
        )
        return []
    return confirmed


def apply_blanket_supersession(
    db: Session,
    client,
    circular: Circular,
    rels: dict,
    *,
    warn=print,
) -> list[Circular]:
    """Create `supersedes` relationships for a blanket supersession, if the LLM flagged one.

    Skips targets already linked as `supersedes` from this circular (e.g. from explicit refs).
    Returns the newly superseded circulars. Caller is responsible for committing / recomputing
    statuses.
    """
    if not rels.get("supersedes_all_previous"):
        return []
    subject = (rels.get("subject") or "").strip()
    if not subject:
        return []

    existing_targets = {
        target_id
        for (target_id,) in db.query(CircularRelationship.target_id).filter(
            CircularRelationship.source_id == circular.id,
            CircularRelationship.type == "supersedes",
            CircularRelationship.target_id.isnot(None),
        )
    }

    superseded = find_blanket_superseded(db, client, circular, subject, warn=warn)
    added: list[Circular] = []
    for target in superseded:
        if target.id in existing_targets:
            continue
        db.add(
            CircularRelationship(
                source_id=circular.id,
                target_id=target.id,
                target_reference=f"(all previous instructions on: {subject})",
                type="supersedes",
                confidence=BLANKET_CONFIDENCE,
            )
        )
        existing_targets.add(target.id)
        added.append(target)
    return added
