"""Read-only circular assessments and durable, explicitly requested maintenance jobs.

Jobs use SyncStatus for their frozen selection and per-item outcomes. They run in
the web process under the circular writer lock; no second Chroma process is used.
"""
import json
import uuid
import threading
from collections import Counter, defaultdict
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import selectinload

from .models import Circular, SyncStatus, SemanticIndexSource, AIGenerationJob
from .mirror import now, dumps
from .circular_ai import GENERATION_FEATURES, run_sync_generation

_cancel_events = {}
_events_lock = threading.Lock()


def cancel_job(identity):
    with _events_lock:
        event = _cancel_events.get(identity)
        if event:
            event.set()


class BatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["attachments", "ai", "index", "body"]
    ids: list[str] = Field(min_length=1, max_length=10000)
    features: list[Literal["summary", "tags", "checklist", "relationships", "entities", "consolidation"]] = Field(default_factory=list)
    size: int = Field(default=50, ge=1, le=500)
    delay: float = Field(default=0.5, ge=0, le=10)
    source_job_id: str | None = None


def ai_done(circular, feature):
    field = {"summary": "summary", "tags": "tags", "checklist": "compliance_checklist"}.get(feature)
    return bool(getattr(circular, f"{feature}_generated_at") or (field and getattr(circular, field)))


def file_present(path):
    from .database import PROJECT_ROOT
    if not path:
        return False
    candidate = (PROJECT_ROOT / path).resolve()
    return candidate.is_relative_to(PROJECT_ROOT.resolve()) and candidate.is_file()


def attachment_state(circular):
    rows = []
    for a in circular.attachments:
        state = ("missing_file" if not file_present(a.local_path) else
                 "extraction_failed" if a.extraction_status in {"error", "failed", "pending"} else
                 "unsupported" if a.extraction_status == "unsupported" else
                 "no_text" if not (a.content_text or "").strip() else "ready")
        rows.append({"id": a.id, "filename": a.filename, "state": state, "error": a.extraction_error})
    state = ("unscanned" if circular.attachments_scanned_at is None else
             "needs_files" if any(a["state"] in {"missing_file", "extraction_failed"} for a in rows) else
             "limited" if any(a["state"] in {"unsupported", "no_text"} for a in rows) else "ready")
    return state, rows


def chain_states(db):
    """Batch the same directed ancestry/descendant walk used by resolve_chain."""
    from .models import CircularRelationship, CircularConsolidation
    from .consolidation import CHAIN_MAX_MEMBERS, CHAIN_RELATIONSHIP_TYPES
    ancestors, descendants = defaultdict(set), defaultdict(set)
    for source, target in db.query(CircularRelationship.source_id, CircularRelationship.target_id).filter(
            CircularRelationship.type.in_(CHAIN_RELATIONSHIP_TYPES), CircularRelationship.target_id.isnot(None)):
        ancestors[source].add(target)
        descendants[target].add(source)
    dates = dict(db.query(Circular.id, Circular.date))
    saved = {c.chain_id: c for c in db.query(CircularConsolidation)}
    def closure(seen, edges):
        frontier = set(seen)
        while frontier and len(seen) <= CHAIN_MAX_MEMBERS:
            frontier = set().union(*(edges.get(i, set()) for i in frontier)) - seen
            seen |= frontier
        return seen
    states = {}
    for identity in set(ancestors) | set(descendants):
        members = closure(closure({identity}, ancestors), descendants)
        ordered = sorted((i for i in members if i in dates), key=lambda i: (dates[i] or datetime.min, i))
        if len(ordered) < 2 or ordered[0] != identity:
            continue
        saved_chain = saved.get(identity)
        states[identity] = ("blocked" if len(ordered) > CHAIN_MAX_MEMBERS else
                            "completed" if saved_chain and not saved_chain.stale and json.loads(saved_chain.member_ids) == ordered else "missing")
    return states


def assess(db, source_job_id=None, ids=None, verify=False):
    from .database import embedding_config, collection
    from .inventory.fingerprint import content_hash, embedding_fingerprint, CHUNKER_VERSION
    from .checklist import prepare_index_chunks
    from .search import _fts_row
    query = db.query(Circular).options(selectinload(Circular.attachments))
    if source_job_id:
        source = db.query(SyncStatus).filter_by(job_id=source_job_id).first()
        if source is None:
            raise ValueError("Unknown source job")
        selection = json.loads(source.selection or "[]")
        selected_ids = [item if isinstance(item, str) else item["id"] for item in selection]
        query = query.filter(Circular.id.in_(selected_ids))
    if ids is not None:
        query = query.filter(Circular.id.in_(ids))
    circulars = query.order_by(Circular.date.desc(), Circular.id).all()
    ledger = {r.source_id: r for r in db.query(SemanticIndexSource).all()}
    fp = embedding_fingerprint(embedding_config)
    incompatible = any(r.embedding_fingerprint and r.embedding_fingerprint != fp for r in ledger.values())
    stored = None
    if verify:
        stored = {}
        def retain(page):
            for key, value in zip(page["ids"], page["documents"]):
                stored.setdefault(key.rsplit("__chunk_", 1)[0], {})[key] = value
        if len(circulars) <= 50:
            for circular in circulars:
                retain(collection.get(where={"circular_id": circular.id}, include=["documents"]))
        else:
            offset = 0
            while True:
                page = collection.get(limit=5000, offset=offset, include=["documents"])
                if not page["ids"]:
                    break
                retain(page)
                offset += len(page["ids"])
    fts_exists = db.execute(text("SELECT name FROM sqlite_master WHERE name='circulars_fts'")).first()
    columns = "circular_id,title,reference,body" if verify else "circular_id"
    fts = {r[0]: tuple(r[1:]) for r in db.execute(text(f"SELECT {columns} FROM circulars_fts"))} if fts_exists else {}
    jobs = db.query(AIGenerationJob).filter(AIGenerationJob.circular_id.isnot(None)).order_by(AIGenerationJob.created_at).all()
    latest = {(j.circular_id, j.feature): j for j in jobs}
    consolidations = chain_states(db)
    result = []
    for c in circulars:
        attachments, details = attachment_state(c)
        ai = {}
        for feature in GENERATION_FEATURES:
            job = latest.get((c.id, feature))
            ai[feature] = ("running" if job and job.status in {"queued", "running"} else
                           "completed" if ai_done(c, feature) else
                           "blocked" if not (c.content_text or "").strip() else
                           "failed" if job and job.status == "failed" else "missing")
        ai["consolidation"] = consolidations.get(c.id, "not_applicable")
        sources = [(c.id, c.content_text or "", "Circular body")]
        sources += [(a.id, a.content_text or "", a.filename) for a in c.attachments]
        vectors = []
        for identity, body, label in sources:
            record = ledger.get(identity)
            state = "blocked" if not body.strip() else "recorded"
            if body.strip():
                if not record or record.status != "indexed":
                    state = "missing" if not record else "stale"
                elif (record.content_hash != content_hash(body) or record.chunker_version != CHUNKER_VERSION
                      or record.embedding_fingerprint != fp):
                    state = "stale"
                elif stored is not None:
                    from .scraper.circulars import circular_document, attachment_document
                    document = (circular_document(c) if identity == c.id else
                                attachment_document(next(a for a in c.attachments if a.id == identity)))
                    document["text"] = body
                    expected = {f"{identity}__chunk_{i}": chunk["text"] for i, chunk in enumerate(prepare_index_chunks(document))}
                    actual = stored.get(identity, {})
                    state = "verified" if expected == actual else "stale"
            vectors.append({"id": identity, "label": label, "state": state})
        keyword = "missing" if c.id not in fts else "recorded"
        if verify and c.id in fts:
            keyword = "verified" if fts[c.id] == _fts_row(c) else "stale"
        result.append({"id": c.id, "reference": c.reference, "title": c.title,
                       "department": c.department, "year": c.date.year if c.date else None,
                       "body": "ready" if (c.content_text or "").strip() else "missing",
                       "attachments": attachments, "files": details, "ai": ai,
                       "keyword": keyword, "vectors": vectors,
                       "index_blocked": incompatible})
    return result


def eligible(row, action, features=()):
    if action == "body":
        return row["body"] == "missing"
    if action == "attachments":
        return row["attachments"] in {"unscanned", "needs_files"}
    if action == "ai":
        return any(row["ai"].get(f) in {"missing", "failed"} for f in features)
    return not row["index_blocked"] and (row["keyword"] in {"missing", "stale"} or
        any(v["state"] in {"missing", "stale"} for v in row["vectors"]))


def create_job(db, request):
    from .circular_jobs import preflight
    preflight(db)
    if request.action == "ai" and not request.features:
        raise ValueError("Select at least one AI feature")
    if db.query(AIGenerationJob.id).filter(AIGenerationJob.circular_id.in_(request.ids),
            AIGenerationJob.status.in_(["queued", "running"])).first():
        raise ValueError("Analysis is already running for a selected circular. Wait for it to finish.")
    rows = assess(db, ids=request.ids, verify=request.action == "index")
    if request.action != "ai" and any(r["index_blocked"] for r in rows):
        raise ValueError("Embedding configuration differs from the stored index. Resolve it before work that writes vectors.")
    selected = [r["id"] for r in rows if eligible(r, request.action, request.features)]
    if not selected:
        raise ValueError("No eligible documents remain in this selection")
    identity = str(uuid.uuid4())
    items = [{"id": i, "status": "pending"} for i in selected]
    if request.action == "ai":
        from .circular_ai import SYNC_GENERATION_FEATURES
        by_id = {row["id"]: row for row in rows}
        items = [{"id": i, "feature": feature, "status": "pending"}
                 for feature in SYNC_GENERATION_FEATURES if feature in request.features
                 for i in selected if by_id[i]["ai"].get(feature) in {"missing", "failed"}]
    job = SyncStatus(job_id=identity, kind="circulars", status="queued", started_at=now(),
                     parameters=dumps({**request.model_dump(exclude={"ids"}), "operation": "maintenance"}),
                     selection=dumps(selected), progress=dumps({"items": items, "cancel_requested": False}))
    db.add(job)
    db.commit()
    with _events_lock:
        _cancel_events[identity] = threading.Event()
    return job


def perform(db, identity, action, features):
    from .scraper.circulars import fetch_attachments_for_circular, vectorize_attachments, _index_circular, vectorize_attachment
    from .search import index_circular_fts
    circular = db.get(Circular, identity)
    if circular is None:
        raise ValueError("Circular no longer present")
    if action == "body":
        from .scraper.circulars import process_circular
        process_circular(db, title=circular.title, url=circular.url,
                         reference=circular.reference or "", department=circular.department or "Unknown",
                         force_fetch=True, include_attachments=False)
        return
    if action == "ai":
        db.close()
        result = run_sync_generation([identity], features)
        if result["errors"]:
            raise RuntimeError("; ".join(result["error_details"]))
        return
    if action == "attachments":
        from .scraper.circulars import process_attachment
        try:
            fetch_attachments_for_circular(db, circular)
            db.expire(circular, ["attachments"])
            for attachment in circular.attachments:
                if (not file_present(attachment.local_path) or
                        attachment.extraction_status in {"pending", "error", "failed"}):
                    process_attachment(db, circular, {"id": attachment.id,
                        "url": attachment.original_url, "filename": attachment.filename,
                        "file_type": attachment.file_type})
        finally:
            index_circular_fts(db, circular)
        db.expire(circular, ["attachments"])
        vectorize_attachments(db, circular)
        state, files = attachment_state(circular)
        if state == "needs_files":
            raise RuntimeError("Some attachments could not be downloaded or extracted")
        if any((a.content_text or "").strip() and not a.is_vectorized for a in circular.attachments):
            raise RuntimeError("Attachment text saved but vector indexing failed")
        return
    row = assess(db, ids=[identity], verify=True)[0]
    if row["index_blocked"]:
        raise ValueError("Embedding configuration differs from the stored index; a full rebuild is required")
    if row["keyword"] in {"missing", "stale"}:
        index_circular_fts(db, circular)
    for source in row["vectors"]:
        if source["state"] not in {"missing", "stale"}:
            continue
        ok = (_index_circular(circular, db=db) if source["id"] == circular.id else
              vectorize_attachment(db, next(a for a in circular.attachments if a.id == source["id"])))
        if not ok:
            raise RuntimeError(f"Index repair failed: {source['label']}")
    checked = assess(db, ids=[identity], verify=True)[0]
    if eligible(checked, "index"):
        raise RuntimeError("Index verification still reports outstanding work")


def run_job(identity, factory):
    from .scraper.http import RequestPacer, http_job
    with factory() as db:
        job = db.query(SyncStatus).filter_by(job_id=identity).one()
        options = json.loads(job.parameters)
        items = json.loads(job.progress)["items"]
        job.status = "running"
        db.commit()
    pacer = RequestPacer(options["delay"])
    with _events_lock:
        cancel = _cancel_events.setdefault(identity, threading.Event())
    try:
        for offset, item in enumerate(items):
            with factory() as db:
                job = db.query(SyncStatus).filter_by(job_id=identity).one()
                if cancel.is_set() or json.loads(job.progress).get("cancel_requested"):
                    break
                item["status"] = "running"
                job.progress = dumps({"items": items, "batch": offset // options["size"] + 1})
                db.commit()
            try:
                with factory() as db, http_job(pacer):
                    perform(db, item["id"], options["action"], [item["feature"]] if item.get("feature") else options["features"])
                item["status"] = "completed"
            except Exception as exc:
                item["status"], item["error"] = "failed", str(exc)
            with factory() as db:
                job = db.query(SyncStatus).filter_by(job_id=identity).one()
                progress = json.loads(job.progress)
                job.progress = dumps({**progress, "items": items, "cancel_requested": cancel.is_set()})
                db.commit()
    finally:
        with factory() as db:
            job = db.query(SyncStatus).filter_by(job_id=identity).one()
            progress = json.loads(job.progress)
            counts = Counter(i["status"] for i in items)
            job.status = "success" if counts["completed"] == len(items) else "failed"
            job.completed_at = now()
            job.processed_count, job.error_count = counts["completed"], counts["failed"]
            job.progress = dumps({**progress, "items": items, "counts": dict(counts), "cancel_requested": cancel.is_set()})
            db.commit()
        with _events_lock:
            _cancel_events.pop(identity, None)


def recover(db):
    for job in db.query(SyncStatus).filter(SyncStatus.status.in_(["queued", "running"])):
        if json.loads(job.parameters or "{}").get("operation") == "maintenance":
            progress = json.loads(job.progress or "{}")
            for item in progress.get("items", []):
                if item["status"] == "running":
                    item["status"], item["error"] = "failed", "Interrupted; verify or retry this item"
            job.progress, job.status, job.error, job.completed_at = dumps(progress), "failed", "Interrupted", now()
    db.commit()
