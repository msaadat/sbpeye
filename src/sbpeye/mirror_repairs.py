"""Targeted index repair from stored text, preserving original attempt evidence."""

import json
import uuid

from .mirror import now, dumps


def _repair_circular(db, circular):
    from .search import index_circular_fts
    from .scraper.circulars import _index_circular, vectorize_attachment
    index_circular_fts(db, circular)
    if not _index_circular(circular, db=db):
        raise RuntimeError("Circular vector indexing failed")
    for attachment in circular.attachments:
        if (attachment.content_text or "").strip() and not vectorize_attachment(db, attachment):
            raise RuntimeError(f"Attachment vector indexing failed: {attachment.id}")


def repair_migration_indexes(manifest):
    from .database import SessionLocal, collection
    from .models import Circular
    with SessionLocal() as db:
        for mapping in manifest["mappings"]:
            circular = db.get(Circular, mapping["new_id"])
            if circular is None:
                raise ValueError("Migrated circular missing")
            _repair_circular(db, circular)
            old = collection.get(where={"circular_id": mapping["old_id"]}, include=["metadatas"])
            removable = [identity for identity, metadata in zip(old["ids"], old["metadatas"])
                         if metadata.get("doc_type") in {"circular", "attachment"} and metadata.get("kind") != "law"]
            if removable:
                collection.delete(ids=removable)
            remaining = collection.get(ids=removable, include=[])["ids"] if removable else []
            if remaining:
                raise RuntimeError("Old circular chunks remain")


def repair_job_indexes(job_id, session_factory):
    from .circular_jobs import preflight
    from .models import Circular, SyncStatus
    from .mirror_models import MirrorAttempt, MirrorGap
    result = {"source_job_id": job_id, "repaired": [], "errors": []}
    with session_factory() as db:
        preflight(db)
        original = db.query(SyncStatus).filter_by(job_id=job_id).first()
        if original is None or json.loads(original.parameters or "{}").get("operation") != "mirror_backfill":
            raise ValueError("Unknown backfill job")
        candidates = set()
        for attempt in db.query(MirrorAttempt).filter_by(job_id=job_id):
            stages = json.loads(attempt.stages or "{}")
            if attempt.outcome == "interrupted" or attempt.error or any(stages.get(key) not in {"ready", "deferred"} for key in ("fts", "vector", "attachments")):
                gap = db.get(MirrorGap, attempt.gap_id)
                if gap and gap.resolution_id:
                    candidates.add(gap.resolution_id)
        repair_id = str(uuid.uuid4())
        row = SyncStatus(job_id=repair_id, kind="circulars", status="running", started_at=now(),
                         parameters=dumps({"operation": "mirror_repair_index", "source_job_id": job_id}), selection=dumps(sorted(candidates)))
        db.add(row)
        db.commit()
    for identity in sorted(candidates):
        try:
            with session_factory() as db:
                circular = db.get(Circular, identity)
                if not circular:
                    raise ValueError("Circular no longer present")
                _repair_circular(db, circular)
            result["repaired"].append(identity)
        except Exception as exc:
            result["errors"].append({"id": identity, "error": str(exc)})
    with session_factory() as db:
        row = db.query(SyncStatus).filter_by(job_id=repair_id).one()
        row.status, row.completed_at = "failed" if result["errors"] else "success", now()
        row.progress, row.error_count, row.processed_count = dumps(result), len(result["errors"]), len(result["repaired"])
        db.commit()
    return result
