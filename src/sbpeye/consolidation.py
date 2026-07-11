"""Amendment-chain consolidation.

A consolidation belongs to a *chain*: a circular's amendment lineage,
discovered deterministically from ``circular_relationships`` rows of type
``amends`` or ``adds_to`` with a resolved target. The lineage is directional:
first the ancestors (everything the circular transitively amends — the base
rules), then every circular that transitively amends or adds to any of those.
An amender's *other* targets are never pulled in, so two rulebooks that merely
share an amending circular stay separate chains. An AI pass then extracts the
base circular's requirements and folds each later circular into a running
consolidated state. The result is one merged requirement list
where every item carries provenance: which circular introduced it, and — when
later modified — the previous value and the amending circular that changed it.

Only page ``content_text`` is consolidated; requirements inside PDF attachments
are out of scope (the payload flags chains that have attachments so the UI can
say so).
"""

import json
import re
from datetime import datetime

from sqlalchemy.orm import Session

from .models import Circular, CircularConsolidation, CircularRelationship

# Chains beyond this size are almost certainly a relationship-extraction error
# (or a hub circular), and each member costs an alignment call — refuse them.
CHAIN_MAX_MEMBERS = 50
CHAIN_RELATIONSHIP_TYPES = ("amends", "adds_to")


def _closure(db: Session, seen: set[str], forward: bool) -> set[str]:
    """Expand ``seen`` along ``amends``/``adds_to`` edges in one direction.

    ``forward=False`` follows source→target (what the members amend);
    ``forward=True`` follows target→source (what amends the members).
    """
    frontier = list(seen)
    while frontier and len(seen) <= CHAIN_MAX_MEMBERS:
        column = (
            CircularRelationship.target_id if forward
            else CircularRelationship.source_id
        )
        rows = db.query(CircularRelationship).filter(
            CircularRelationship.type.in_(CHAIN_RELATIONSHIP_TYPES),
            CircularRelationship.target_id.isnot(None),
            column.in_(frontier),
        ).all()
        next_frontier: list[str] = []
        for row in rows:
            member_id = row.source_id if forward else row.target_id
            if member_id and member_id not in seen:
                seen.add(member_id)
                next_frontier.append(member_id)
        frontier = next_frontier
    return seen


def resolve_chain(db: Session, circular_id: str) -> list[Circular]:
    """The circular's amendment lineage along ``amends``/``adds_to`` edges.

    Walks backwards first (everything the circular transitively amends), then
    forwards from that set (everything that transitively amends or adds to a
    member). Sideways hops are excluded: an amender's other targets do not
    join, so rulebooks that share an amending circular remain separate chains.
    Returns members ordered oldest-first (the first element is the base
    circular and defines the chain id); a list of fewer than two members means
    the circular is not part of an amendment chain.
    """
    seen = _closure(db, {circular_id}, forward=False)
    seen = _closure(db, seen, forward=True)

    members = db.query(Circular).filter(Circular.id.in_(seen)).all()
    members.sort(key=lambda item: (item.date or datetime.min, item.id))
    return members


def _normalize_value(value: str) -> str:
    return re.sub(r"[\s,]+", "", str(value or "")).casefold()


def value_supported(value: str, source_text: str) -> bool:
    """Whether a claimed value appears (whitespace/case-insensitively) in the
    amending circular's text. Diff-critical values that fail this check are
    marked low-confidence rather than dropped."""
    needle = _normalize_value(value)
    if not needle:
        return True
    return needle in _normalize_value(source_text)


def _member_summary(member: Circular) -> dict:
    return {
        "id": member.id,
        "reference": member.reference,
        "title": member.title,
        "date": member.date.strftime("%Y-%m-%d") if member.date else None,
        "status": member.status or "active",
        "has_attachments": bool(member.attachments),
    }


def _next_req_id(state: list[dict]) -> str:
    highest = 0
    for item in state:
        match = re.fullmatch(r"r(\d+)", str(item.get("req_id") or ""))
        if match:
            highest = max(highest, int(match.group(1)))
    return f"r{highest + 1}"


def _apply_changes(state: list[dict], changes: list[dict], amender: Circular) -> None:
    """Fold one amending circular's change list into the consolidated state.

    ``old_*`` fields come from our own running state, never from the model, so
    the "previous value" side of a diff cannot be hallucinated. New values are
    checked against the amender's text and demoted to low confidence on a miss.
    """
    by_id = {item["req_id"]: item for item in state}
    source_text = amender.content_text or ""
    for change in changes:
        action = change.get("action")
        if action == "add":
            requirement = str(change.get("requirement") or "").strip()
            if not requirement:
                continue
            value = str(change.get("value") or "").strip()
            state.append({
                "req_id": _next_req_id(state),
                "section": str(change.get("section") or "").strip(),
                "text": requirement,
                "value": value,
                "applies_to": str(change.get("applies_to") or "").strip(),
                "status": "added",
                "introduced_by": amender.id,
                "last_changed_by": amender.id,
                "old_text": None,
                "old_value": None,
                "removed_by": None,
                "confidence": "high" if value_supported(value, source_text) else "low",
                "history": [{"circular_id": amender.id, "action": "added"}],
            })
            continue

        item = by_id.get(str(change.get("req_id") or ""))
        if item is None or item.get("status") == "removed":
            continue
        if action == "remove":
            item["status"] = "removed"
            item["removed_by"] = amender.id
            item["history"].append({"circular_id": amender.id, "action": "removed"})
        elif action == "modify":
            new_text = str(change.get("requirement") or "").strip()
            new_value = str(change.get("value") or "").strip()
            if not new_text and not new_value:
                continue
            item["old_text"] = item["text"]
            item["old_value"] = item.get("value") or None
            if new_text:
                item["text"] = new_text
            if new_value:
                item["value"] = new_value
            if item.get("status") != "added":
                item["status"] = "modified"
            item["last_changed_by"] = amender.id
            # Confidence tracks the *current* value: each modification re-verifies
            # against the circular that set it.
            item["confidence"] = "high" if value_supported(new_value, source_text) else "low"
            item["history"].append({
                "circular_id": amender.id,
                "action": "modified",
                "old_value": item["old_value"],
                "new_value": item.get("value") or None,
            })


def generate_consolidation(
    db: Session,
    client,
    circular: Circular,
    progress_callback=None,
) -> CircularConsolidation:
    """Extract the base circular's requirements and fold each amendment in,
    then persist (upsert) the chain's consolidation row."""
    members = resolve_chain(db, circular.id)
    if len(members) < 2:
        raise ValueError(
            "This circular has no resolved amendment chain to consolidate. "
            "Generate relationships first if the circular text references amendments."
        )
    if len(members) > CHAIN_MAX_MEMBERS:
        raise ValueError(
            f"The amendment chain has more than {CHAIN_MAX_MEMBERS} circulars; "
            "this usually indicates a relationship extraction error."
        )
    missing = [item.display_name for item in members if not item.content_text]
    if missing:
        raise ValueError(
            "These chain members have no extracted content: " + ", ".join(missing)
        )

    base, amenders = members[0], members[1:]
    total = len(members)

    def report(completed: int) -> None:
        if progress_callback:
            progress_callback(completed, total)

    report(0)
    extracted = client.extract_requirements(
        base.title, base.reference or "", base.content_text
    )
    state: list[dict] = []
    for index, entry in enumerate(extracted, start=1):
        value = str(entry.get("value") or "").strip()
        state.append({
            "req_id": f"r{index}",
            "section": str(entry.get("section") or "").strip(),
            "text": str(entry.get("requirement") or "").strip(),
            "value": value,
            "applies_to": str(entry.get("applies_to") or "").strip(),
            "status": "unchanged",
            "introduced_by": base.id,
            "last_changed_by": None,
            "old_text": None,
            "old_value": None,
            "removed_by": None,
            "confidence": "high" if value_supported(value, base.content_text or "") else "low",
            "history": [],
        })
    if not state:
        raise ValueError("No requirements could be extracted from the base circular.")
    report(1)

    for offset, amender in enumerate(amenders, start=2):
        changes = client.align_requirements(
            current_requirements=[
                {
                    "req_id": item["req_id"],
                    "section": item["section"],
                    "requirement": item["text"],
                    "value": item["value"],
                    "removed": item["status"] == "removed",
                }
                for item in state
            ],
            amending_reference=amender.reference or "",
            amending_title=amender.title,
            amending_text=amender.content_text,
        )
        _apply_changes(state, changes, amender)
        report(offset)

    row = db.query(CircularConsolidation).filter(
        CircularConsolidation.chain_id == base.id
    ).first()
    if row is None:
        row = CircularConsolidation(chain_id=base.id)
        db.add(row)
    row.member_ids = json.dumps([item.id for item in members])
    row.as_of_circular_id = members[-1].id
    row.requirements = json.dumps(state)
    row.model = getattr(getattr(client, "config", None), "effective_chat_model", None)
    row.stale = 0
    row.generated_at = datetime.utcnow()
    return row


def mark_stale(db: Session, circular_ids: set[str]) -> None:
    """Flag consolidations touching any of the given circulars as stale.

    Called after a circular's relationships are rewritten: the chain may have
    gained a member, so the stored merge no longer reflects the latest state.
    """
    if not circular_ids:
        return
    for row in db.query(CircularConsolidation).all():
        try:
            members = set(json.loads(row.member_ids or "[]"))
        except (TypeError, ValueError):
            members = set()
        if members & circular_ids or row.chain_id in circular_ids:
            row.stale = 1


def consolidation_payload(db: Session, circular: Circular) -> dict:
    """API payload for a circular's chain consolidation (shared by every
    member of the chain)."""
    members = resolve_chain(db, circular.id)
    if len(members) < 2:
        return {"available": False, "chain": [], "consolidation": None}

    base = members[0]
    member_ids = [item.id for item in members]
    row = db.query(CircularConsolidation).filter(
        CircularConsolidation.chain_id == base.id
    ).first()
    payload = {
        "available": True,
        "chain_id": base.id,
        "chain": [_member_summary(item) for item in members],
        "has_attachments": any(bool(item.attachments) for item in members),
        "consolidation": None,
    }
    if row is not None:
        try:
            stored_members = json.loads(row.member_ids or "[]")
        except (TypeError, ValueError):
            stored_members = []
        try:
            requirements = json.loads(row.requirements or "[]")
        except (TypeError, ValueError):
            requirements = []
        payload["consolidation"] = {
            "as_of_circular_id": row.as_of_circular_id,
            "member_ids": stored_members,
            "requirements": requirements,
            "model": row.model,
            "generated_at": row.generated_at.isoformat() if row.generated_at else None,
            # A chain that has grown since generation is stale even if the
            # relationships pass never flagged it explicitly.
            "stale": bool(row.stale) or stored_members != member_ids,
        }
    return payload
