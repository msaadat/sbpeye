"""Preserve LLM-generated data across the July-2026 site-redesign rebuild.

The redesign moves circular identity from a URL-derived id to a reference-derived id and
re-scrapes every circular from the new site. Scraped content (text, attachments) is cheap
to reproduce, but the LLM outputs (summaries, tags, checklists, relationships, entities)
are expensive, so we snapshot them keyed by the *normalized reference* — the one identifier
that is stable across the old and new sites — and reattach them to the freshly scraped rows.

Typical flow:

    snapshot = snapshot_llm_data(db)      # before wiping
    wipe_circular_data(db)                 # drop old rows + vectors
    scrape_circulars(db, ...)              # fresh rows, reference-based ids
    apply_llm_snapshot(db, snapshot)       # reattach preserved outputs
    # then: sbpeye reindex
"""

from datetime import datetime

from sqlalchemy.orm import Session

from .circular_ai import _recompute_statuses, _resolve_reference
from .link_routing import normalize_reference
from .models import Circular, CircularEntity, CircularRelationship

# Circular columns carrying preserved LLM output (copied verbatim on reattach).
_PRESERVED_FIELDS = (
    "summary",
    "tags",
    "compliance_checklist",
    "status",
    "summary_generated_at",
    "tags_generated_at",
    "checklist_generated_at",
    "relationships_generated_at",
    "entities_generated_at",
)
# CircularEntity columns to snapshot (everything except identity/foreign-key/audit).
_ENTITY_FIELDS = (
    "entity_type", "metric", "comparator", "value_numeric", "value_high", "unit",
    "value_text", "subject", "effective_date", "context_snippet", "source_unit_id",
    "page_start", "confidence",
)


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else None


def _dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def snapshot_llm_data(db: Session) -> dict:
    """Export preserved LLM data keyed by normalized reference.

    Circulars without a parseable reference cannot be matched back after the rebuild and
    are reported under ``"unkeyed"`` so the operator can review them.
    """
    circulars: dict[str, dict] = {}
    unkeyed: list[str] = []

    for circular in db.query(Circular):
        key = normalize_reference(circular.reference)
        if not key:
            unkeyed.append(circular.reference or circular.title or circular.id)
            continue

        payload: dict = {"reference": circular.reference, "old_url": circular.url}
        for field in _PRESERVED_FIELDS:
            value = getattr(circular, field)
            payload[field] = _iso(value) if isinstance(value, datetime) else value

        payload["relationships"] = [
            {
                "target_reference": rel.target_reference,
                "type": rel.type,
                "confidence": rel.confidence,
            }
            for rel in circular.amends
            if rel.target_reference
        ]
        payload["entities"] = [
            {
                field: (_iso(getattr(entity, field)) if field == "effective_date"
                        else getattr(entity, field))
                for field in _ENTITY_FIELDS
            }
            for entity in circular.entities
        ]
        # Last write wins if two circulars normalize to the same key (should not happen).
        circulars[key] = payload

    return {"circulars": circulars, "unkeyed": unkeyed}


def apply_llm_snapshot(db: Session, snapshot: dict) -> dict:
    """Reattach preserved LLM data to freshly scraped circulars, matched by reference.

    Returns counts of matched/unmatched circulars and rebuilt relationships/entities.
    ``unmatched_snapshot`` lists snapshot keys with no circular in the rebuilt DB.
    """
    preserved: dict[str, dict] = snapshot.get("circulars", {})
    matched_keys: set[str] = set()
    entities_created = 0
    # (source_circular, [relationship payloads]) collected for a second resolution pass.
    staged_relationships: list[tuple[Circular, list[dict]]] = []

    for circular in db.query(Circular):
        key = normalize_reference(circular.reference)
        payload = preserved.get(key) if key else None
        if not payload:
            continue
        matched_keys.add(key)

        for field in _PRESERVED_FIELDS:
            value = payload.get(field)
            if field.endswith("_at"):
                value = _dt(value)
            setattr(circular, field, value)
        if payload.get("old_url"):
            circular.old_url = payload["old_url"]

        db.query(CircularEntity).filter(
            CircularEntity.circular_id == circular.id
        ).delete(synchronize_session=False)
        for entity in payload.get("entities", []):
            fields = dict(entity)
            fields["effective_date"] = _dt(fields.get("effective_date"))
            db.add(CircularEntity(circular_id=circular.id, **fields))
            entities_created += 1

        if payload.get("relationships"):
            staged_relationships.append((circular, payload["relationships"]))

    # Resolve relationship targets against the rebuilt corpus in a second pass so targets
    # scraped after their source are still found.
    relationships_created = 0
    for source, relationships in staged_relationships:
        db.query(CircularRelationship).filter(
            CircularRelationship.source_id == source.id
        ).delete(synchronize_session=False)
        for rel in relationships:
            target = _resolve_reference(db, str(rel["target_reference"]), current=source)
            db.add(CircularRelationship(
                source_id=source.id,
                target_id=target.id if target else None,
                target_reference=rel["target_reference"],
                type=rel["type"],
                confidence=rel.get("confidence"),
            ))
            relationships_created += 1

    db.flush()
    _recompute_statuses(db)
    db.commit()

    return {
        "matched": len(matched_keys),
        "unmatched_snapshot": sorted(set(preserved) - matched_keys),
        "relationships": relationships_created,
        "entities": entities_created,
    }


def wipe_circular_data(db: Session) -> None:
    """Delete all circulars (and their relationships/entities/attachments) plus vectors.

    Used before a from-scratch re-scrape so the new reference-based ids do not collide
    with or orphan the old URL-based rows.
    """
    from .database import collection

    # db.query(CircularRelationship).delete(synchronize_session=False)
    # db.query(CircularEntity).delete(synchronize_session=False)
    # from .models import Attachment
    # db.query(Attachment).delete(synchronize_session=False)
    # db.query(Circular).delete(synchronize_session=False)
    # db.commit()

    existing = collection.get(include=[]).get("ids", [])
    if existing:
        collection.delete(ids=existing)
