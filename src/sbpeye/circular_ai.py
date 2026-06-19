import json
from datetime import datetime

from sqlalchemy import or_
from sqlalchemy.orm import Session

from .ai import get_ai_client
from .database import SessionLocal
from .models import AIGenerationJob, Circular, CircularRelationship


GENERATION_FEATURES = ("summary", "tags", "checklist", "relationships")
GENERATION_ACTIONS = (*GENERATION_FEATURES, "all")


def _resolve_reference(db: Session, reference: str) -> Circular | None:
    reference = reference.strip()
    if not reference:
        return None
    matches = db.query(Circular).filter(
        or_(
            Circular.reference.ilike(f"%{reference}%"),
            Circular.title.ilike(f"%{reference}%"),
        )
    ).limit(5).all()
    if len(matches) == 1:
        return matches[0]
    reference_lower = reference.lower()
    return next(
        (item for item in matches if item.reference and item.reference.lower() == reference_lower),
        None,
    )


def _recompute_statuses(db: Session) -> None:
    status_map: dict[str, str] = {}
    priority = {"cancelled": 3, "superseded": 2, "amended": 1, "active": 0}
    for relationship in db.query(CircularRelationship).filter(
        CircularRelationship.target_id.isnot(None)
    ):
        target_status = {
            "supersedes": "superseded",
            "cancels": "cancelled",
        }.get(relationship.type, "amended")
        current = status_map.get(relationship.target_id, "active")
        if priority[target_status] > priority[current]:
            status_map[relationship.target_id] = target_status

    for circular in db.query(Circular):
        circular.status = status_map.get(circular.id, "active")


def _compute_outputs(client, circular: Circular, feature: str) -> dict:
    features = GENERATION_FEATURES if feature == "all" else (feature,)
    outputs: dict = {}
    for item in features:
        if item == "summary":
            summary = client.summarize(circular.title, circular.content_text)
            if not summary:
                raise ValueError("The model returned an empty summary.")
            outputs[item] = summary
        elif item == "tags":
            outputs[item] = client.generate_tags(circular.title, circular.content_text)
        elif item == "checklist":
            outputs[item] = client.generate_checklist(circular.title, circular.content_text)
        elif item == "relationships":
            outputs[item] = client.extract_relationships(
                circular.title,
                circular.reference or "",
                circular.content_text,
            )
    return outputs


def _persist_outputs(db: Session, circular: Circular, outputs: dict) -> None:
    generated_at = datetime.utcnow()
    if "summary" in outputs:
        circular.summary = outputs["summary"]
        circular.summary_generated_at = generated_at
    if "tags" in outputs:
        circular.tags = json.dumps(outputs["tags"])
        circular.tags_generated_at = generated_at
    if "checklist" in outputs:
        circular.compliance_checklist = json.dumps(outputs["checklist"])
        circular.checklist_generated_at = generated_at
    if "relationships" in outputs:
        db.query(CircularRelationship).filter(
            CircularRelationship.source_id == circular.id
        ).delete(synchronize_session=False)
        relationships = outputs["relationships"]
        for relationship_type in ("amends", "supersedes", "cancels", "adds_to", "clarifies"):
            for target_reference in relationships.get(relationship_type, []):
                target = _resolve_reference(db, str(target_reference))
                db.add(CircularRelationship(
                    source_id=circular.id,
                    target_id=target.id if target else None,
                    target_reference=str(target_reference),
                    type=relationship_type,
                ))
        circular.relationships_generated_at = generated_at
        db.flush()
        _recompute_statuses(db)


def run_generation_job(job_id: str) -> None:
    db = SessionLocal()
    try:
        job = db.query(AIGenerationJob).filter(AIGenerationJob.id == job_id).first()
        if not job:
            return
        job.status = "running"
        job.started_at = datetime.utcnow()
        db.commit()

        circular = db.query(Circular).filter(Circular.id == job.circular_id).first()
        if not circular or not circular.content_text:
            raise ValueError("This circular has no extracted content to analyze.")

        client = get_ai_client(db)
        outputs = _compute_outputs(client, circular, job.feature)
        _persist_outputs(db, circular, outputs)
        job.status = "succeeded"
        job.completed_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        db.rollback()
        job = db.query(AIGenerationJob).filter(AIGenerationJob.id == job_id).first()
        if job:
            job.status = "failed"
            job.error = str(exc)
            job.completed_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()


def generation_job_payload(job: AIGenerationJob) -> dict:
    return {
        "id": job.id,
        "circular_id": job.circular_id,
        "feature": job.feature,
        "status": job.status,
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }
