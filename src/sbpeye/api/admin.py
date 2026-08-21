"""Read-only operator visibility: what the corpus holds and what the index can reach.

Everything here answers a question an administrator could previously only answer by
opening a shell on the machine holding the corpus and running `sbpeye stats`,
`sbpeye laws status` or `sbpeye inventory status`. The deployment cannot reach SBP
(deployment plan 2.1), so corpus *writes* happen on a maintainer's machine — but
knowing what is in the corpus, what is searchable and what has run is exactly the thing
you want from wherever you are.

**Every route in this module is read-only.** No route writes to the corpus, the vector
store or the ledger — `/index/audit` runs the reconciler with `write=False` precisely so
that looking at the index cannot change it. Anything that repairs, re-indexes or
generates stays on the CLI for now.

Sessions are corpus sessions throughout (invariant 3.1): every table read here —
circulars, attachments, reg documents, the ledger, sync rows, generation jobs — lives in
`sbpeye.db`.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, text
from sqlalchemy.orm import Session

from .. import database as database_module
from ..auth_routes import require_admin
from ..database import get_db
from ..env import (
    CIRCULAR_FILES_DIR,
    DATA_ROOT,
    FILES_CACHE_DIR,
    LAWS_ARCHIVE_DIR,
)
from ..inventory.fingerprint import CHUNKER_VERSION, embedding_fingerprint
from ..llm_debug import debug_allowed
from ..models import (
    AIGenerationJob,
    Attachment,
    Circular,
    CircularConsolidation,
    CircularEntity,
    CircularRelationship,
    RegDocument,
    RegDocumentLink,
    RegDocumentVersion,
    SemanticIndexSource,
    SyncStatus,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)

# The live audit walks every chunk in the vector store. It writes nothing, but it is the
# one expensive route here, and two admins refreshing the tab at once would page the
# whole collection twice for the same answer. Non-blocking, like the sync lock in `main`.
_AUDIT_LOCK = threading.Lock()

# The AI outputs a circular can carry, paired with the column that records when each was
# produced. Presence of the timestamp is the coverage signal rather than presence of the
# text: an empty summary column is ambiguous between "never generated" and "generated
# and the model returned nothing", and only the first is a gap worth acting on.
CIRCULAR_FEATURES = (
    ("summary", Circular.summary_generated_at),
    ("tags", Circular.tags_generated_at),
    ("checklist", Circular.checklist_generated_at),
    ("relationships", Circular.relationships_generated_at),
    ("entities", Circular.entities_generated_at),
)

LAW_VERSION_FEATURES = (
    ("summary", RegDocumentVersion.summary_generated_at),
    ("tags", RegDocumentVersion.tags_generated_at),
    ("checklist", RegDocumentVersion.checklist_generated_at),
    ("relationships", RegDocumentVersion.relationships_generated_at),
    ("entities", RegDocumentVersion.entities_generated_at),
)


def _iso(value) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _counts(rows) -> dict[str, int]:
    """`[(label, count), ...]` from a group-by into a dict, with NULL as "unknown"."""
    return {(label or "unknown"): count for label, count in rows}


def _facets(rows) -> list[dict]:
    """A group-by result as an ordered list, largest first — for tables, not lookups."""
    return [
        {"label": label or "unknown", "count": count}
        for label, count in sorted(rows, key=lambda row: (-row[1], str(row[0] or "")))
    ]


# --------------------------------------------------------------------------- corpus


def _circular_section(db: Session) -> dict:
    total = db.query(func.count(Circular.id)).scalar() or 0
    latest = db.query(Circular).order_by(Circular.date.desc()).first()

    return {
        "total": total,
        "by_status": _counts(
            db.query(Circular.status, func.count(Circular.id))
            .group_by(Circular.status)
            .all()
        ),
        "by_department": _facets(
            db.query(Circular.department, func.count(Circular.id))
            .group_by(Circular.department)
            .all()
        ),
        "by_year": [
            {"label": label or "unknown", "count": count}
            for label, count in db.query(
                func.strftime("%Y", Circular.date), func.count(Circular.id)
            )
            .group_by(func.strftime("%Y", Circular.date))
            .order_by(func.strftime("%Y", Circular.date).desc())
            .all()
        ],
        "earliest_date": _iso(db.query(func.min(Circular.date)).scalar()),
        "latest_date": _iso(db.query(func.max(Circular.date)).scalar()),
        "latest": (
            {
                "id": latest.id,
                "reference": latest.reference,
                "title": latest.title,
                "department": latest.department,
                "date": _iso(latest.date),
            }
            if latest
            else None
        ),
        "indexed_today": db.query(func.count(Circular.id))
        .filter(func.date(Circular.indexed_at) == datetime.utcnow().date())
        .scalar()
        or 0,
        # Touched by *any* analysis feature. Distinct from the per-feature coverage
        # below: a circular with tags and nothing else is analysed but far from complete,
        # and the console has to be able to say both things.
        "analysed": db.query(func.count(Circular.id))
        .filter(or_(*[column.isnot(None) for _, column in CIRCULAR_FEATURES]))
        .scalar()
        or 0,
        "coverage": [
            {
                "feature": feature,
                "generated": db.query(func.count(Circular.id))
                .filter(column.isnot(None))
                .scalar()
                or 0,
                "total": total,
            }
            for feature, column in CIRCULAR_FEATURES
        ],
    }


def _attachment_section(db: Session) -> dict:
    total = db.query(func.count(Attachment.id)).scalar() or 0
    return {
        "total": total,
        "by_extraction_status": _counts(
            db.query(Attachment.extraction_status, func.count(Attachment.id))
            .group_by(Attachment.extraction_status)
            .all()
        ),
        "by_file_type": _facets(
            db.query(Attachment.file_type, func.count(Attachment.id))
            .group_by(Attachment.file_type)
            .all()
        ),
        "vectorized": db.query(func.count(Attachment.id))
        .filter(Attachment.is_vectorized == 1)
        .scalar()
        or 0,
        "with_text": db.query(func.count(Attachment.id))
        .filter(Attachment.content_text.isnot(None), Attachment.content_text != "")
        .scalar()
        or 0,
        "with_error": db.query(func.count(Attachment.id))
        .filter(Attachment.extraction_error.isnot(None))
        .scalar()
        or 0,
    }


def _law_section(db: Session) -> dict:
    documents = db.query(func.count(RegDocument.id)).scalar() or 0
    containers = db.query(func.count(RegDocument.id)).filter(RegDocument.children.any()).scalar() or 0
    current_versions = (
        db.query(func.count(RegDocumentVersion.id))
        .filter(RegDocumentVersion.is_current == 1)
        .scalar()
        or 0
    )

    # A container's summary and tags live on the document row (its own version is a
    # manifest with no text); everything else belongs to the edition in force. Same rule
    # the CLI and the API read through — see `cli.commands._law_feature_done_at`.
    container_rollups = {
        "summary": db.query(func.count(RegDocument.id))
        .filter(RegDocument.children.any(), RegDocument.summary_generated_at.isnot(None))
        .scalar()
        or 0,
        "tags": db.query(func.count(RegDocument.id))
        .filter(RegDocument.children.any(), RegDocument.tags_generated_at.isnot(None))
        .scalar()
        or 0,
    }

    return {
        "documents": documents,
        "containers": containers,
        "parts": db.query(func.count(RegDocument.id))
        .filter(RegDocument.parent_id.isnot(None))
        .scalar()
        or 0,
        "versions": db.query(func.count(RegDocumentVersion.id)).scalar() or 0,
        "current_versions": current_versions,
        "not_yet_in_force": db.query(func.count(RegDocumentVersion.id))
        .filter(
            RegDocumentVersion.effective_from.isnot(None),
            RegDocumentVersion.is_current == 0,
            RegDocumentVersion.effective_from > datetime.utcnow(),
        )
        .scalar()
        or 0,
        # Rows that resolved to a circular SBPEye already holds: content lives on the
        # circular, never duplicated here.
        "circular_backed": db.query(func.count(RegDocument.id))
        .filter(RegDocument.circular_id.isnot(None))
        .scalar()
        or 0,
        "external": db.query(func.count(RegDocument.id))
        .filter(RegDocument.is_external == 1)
        .scalar()
        or 0,
        "delisted": db.query(func.count(RegDocument.id))
        .filter(RegDocument.delisted_at.isnot(None))
        .scalar()
        or 0,
        # Still waiting for content, and not deliberately content-free.
        "stubs": db.query(func.count(RegDocument.id))
        .filter(
            ~RegDocument.versions.any(),
            RegDocument.is_external == 0,
            RegDocument.circular_id.is_(None),
        )
        .scalar()
        or 0,
        "vectorized_versions": db.query(func.count(RegDocumentVersion.id))
        .filter(RegDocumentVersion.is_current == 1, RegDocumentVersion.is_vectorized == 1)
        .scalar()
        or 0,
        # Editions in force carrying any analysis. Counted against versions rather than
        # documents because that is where law analysis is stored — see the coverage note.
        "analysed": db.query(func.count(RegDocumentVersion.id))
        .filter(
            RegDocumentVersion.is_current == 1,
            or_(*[column.isnot(None) for _, column in LAW_VERSION_FEATURES]),
        )
        .scalar()
        or 0,
        "by_doc_type": _facets(
            db.query(RegDocument.doc_type, func.count(RegDocument.id))
            .group_by(RegDocument.doc_type)
            .all()
        ),
        "by_extraction_status": _counts(
            db.query(
                RegDocumentVersion.extraction_status, func.count(RegDocumentVersion.id)
            )
            .group_by(RegDocumentVersion.extraction_status)
            .all()
        ),
        "circular_links": db.query(func.count(RegDocumentLink.id)).scalar() or 0,
        "coverage": [
            {
                "feature": feature,
                "generated": (
                    db.query(func.count(RegDocumentVersion.id))
                    .filter(RegDocumentVersion.is_current == 1, column.isnot(None))
                    .scalar()
                    or 0
                )
                + container_rollups.get(feature, 0),
                "total": current_versions + (containers if feature in container_rollups else 0),
            }
            for feature, column in LAW_VERSION_FEATURES
        ],
    }


@router.get("/corpus")
def corpus_overview(db: Session = Depends(get_db)) -> dict:
    """What the corpus holds, and how much of it has been analysed.

    The counts the CLI prints from `sbpeye stats` and `sbpeye laws status`, returned
    structured instead of formatted. Pure SQL over the corpus — no vector store, no
    network — so it is cheap enough to be the tab that loads first.
    """
    relationships = db.query(func.count(CircularRelationship.id)).scalar() or 0
    return {
        "generated_at": datetime.utcnow().isoformat(),
        "circulars": _circular_section(db),
        "attachments": _attachment_section(db),
        "laws": _law_section(db),
        "relationships": {
            "total": relationships,
            # An edge whose target could not be resolved to a row we hold: the reference
            # was extracted but names a circular outside the corpus, or one whose
            # identity did not match. `circulars resolve-targets` is what closes these.
            "resolved": db.query(func.count(CircularRelationship.id))
            .filter(CircularRelationship.target_id.isnot(None))
            .scalar()
            or 0,
            "by_type": _counts(
                db.query(CircularRelationship.type, func.count(CircularRelationship.id))
                .group_by(CircularRelationship.type)
                .all()
            ),
        },
        "entities": {
            "total": db.query(func.count(CircularEntity.id)).scalar() or 0,
            "by_subject_kind": _counts(
                db.query(CircularEntity.subject_kind, func.count(CircularEntity.id))
                .group_by(CircularEntity.subject_kind)
                .all()
            ),
        },
        "consolidations": {
            "total": db.query(func.count(CircularConsolidation.chain_id)).scalar() or 0,
            "stale": db.query(func.count(CircularConsolidation.chain_id))
            .filter(CircularConsolidation.stale == 1)
            .scalar()
            or 0,
        },
    }


# ---------------------------------------------------------------------------- index


def _fts_count(db: Session, table: str) -> int | None:
    """Row count of an FTS5 table, or None if it has not been created yet.

    A fresh database has no virtual tables until `backfill_fts` runs, and that is a
    legible state rather than an error — the caller renders it as "not built".
    """
    try:
        return db.execute(text(f"SELECT count(*) FROM {table}")).scalar() or 0
    except Exception:
        return None


def _vector_store_state() -> tuple[int | None, str]:
    try:
        count = database_module.collection.count()
    except Exception as exc:
        logger.exception("Vector store is unreadable")
        # The class, not the message: a Chroma error carries the store path, and while
        # this route is admin-gated the same restraint as `/healthz` costs nothing.
        return None, f"error: {type(exc).__name__}"
    return count, "ok" if count else "empty"


def _embedding_section() -> dict:
    config = database_module.embedding_config
    return {
        "provider": config.provider,
        "model": config.model,
        "fingerprint": embedding_fingerprint(config),
        "chunker_version": CHUNKER_VERSION,
    }


@router.get("/index")
def index_overview(db: Session = Depends(get_db)) -> dict:
    """Index health as currently *recorded*, without re-reading the vector store.

    This is the ledger's own account of itself — `semantic_index_sources` rows written by
    the last sync or audit — plus the store's chunk total and the lexical index sizes.
    Fast, and honest about being a record rather than a measurement: `/index/audit`
    is what compares the record against the store.

    The fingerprint comparison is the one to watch. The uploaded Chroma index was built
    with a specific embedding model, and running against a different one returns nonsense
    rather than an error (deployment plan 2.2). A mismatch here is that failure, named.
    """
    embedding = _embedding_section()
    chunk_total, store_state = _vector_store_state()

    rows = db.query(func.count(SemanticIndexSource.id)).scalar() or 0
    by_status = _counts(
        db.query(SemanticIndexSource.status, func.count(SemanticIndexSource.id))
        .group_by(SemanticIndexSource.status)
        .all()
    )
    recorded_fingerprints = [
        value
        for (value,) in db.query(SemanticIndexSource.embedding_fingerprint)
        .filter(SemanticIndexSource.embedding_fingerprint.isnot(None))
        .distinct()
        .all()
    ]
    recorded_chunkers = [
        value
        for (value,) in db.query(SemanticIndexSource.chunker_version)
        .filter(SemanticIndexSource.chunker_version.isnot(None))
        .distinct()
        .all()
    ]

    return {
        "generated_at": datetime.utcnow().isoformat(),
        "embedding": embedding,
        "ledger": {
            "rows": rows,
            "by_status": by_status,
            "by_source_kind": _counts(
                db.query(SemanticIndexSource.source_kind, func.count(SemanticIndexSource.id))
                .group_by(SemanticIndexSource.source_kind)
                .all()
            ),
            "expected_chunks": db.query(
                func.coalesce(func.sum(SemanticIndexSource.expected_chunks), 0)
            ).scalar()
            or 0,
            "indexed_chunks": db.query(
                func.coalesce(func.sum(SemanticIndexSource.indexed_chunks), 0)
            ).scalar()
            or 0,
            "searchable": by_status.get("indexed", 0) + by_status.get("stale", 0),
            "last_indexed_at": _iso(
                db.query(func.max(SemanticIndexSource.indexed_at)).scalar()
            ),
            "with_error": db.query(func.count(SemanticIndexSource.id))
            .filter(SemanticIndexSource.error.isnot(None))
            .scalar()
            or 0,
        },
        "vector_store": {
            "state": store_state,
            "chunks": chunk_total,
            "path": str(database_module.CHROMA_DB_DIR),
        },
        "fts": {
            "circulars": _fts_count(db, "circulars_fts"),
            "laws": _fts_count(db, "laws_fts"),
        },
        "drift": {
            # More than one recorded value means the ledger describes an index built in
            # more than one configuration — a partial re-index, not a clean one.
            "recorded_fingerprints": recorded_fingerprints,
            "recorded_chunker_versions": recorded_chunkers,
            "fingerprint_matches": (
                recorded_fingerprints == [embedding["fingerprint"]]
                if recorded_fingerprints
                else None
            ),
            "chunker_matches": (
                recorded_chunkers == [CHUNKER_VERSION] if recorded_chunkers else None
            ),
        },
    }


@router.get("/index/audit")
def index_audit(db: Session = Depends(get_db)):
    """Compare the ledger against what is actually stored in the vector store.

    `reconcile(write=False)`: it reads Chroma and the corpus and writes nothing, so
    looking at the index can never change it. Repairing is `sbpeye inventory index
    --repair`, deliberately still a CLI action.

    Costs a full pass over the collection — tens of thousands of chunks — which is why it
    is a separate route the operator asks for rather than part of `/index`.
    """
    if not _AUDIT_LOCK.acquire(blocking=False):
        return JSONResponse(
            {"error": "An index audit is already running. Try again in a moment."},
            status_code=409,
        )
    try:
        from ..inventory.ledger import reconcile

        started = datetime.utcnow()
        report = reconcile(
            db, database_module.collection, database_module.embedding_config, write=False
        )
        duration_ms = int((datetime.utcnow() - started).total_seconds() * 1000)
    except Exception as exc:
        logger.exception("Index audit failed")
        return JSONResponse(
            {"error": f"The audit could not complete: {type(exc).__name__}: {exc}"},
            status_code=500,
        )
    finally:
        _AUDIT_LOCK.release()

    return {
        "generated_at": datetime.utcnow().isoformat(),
        "duration_ms": duration_ms,
        "is_complete": report.is_complete,
        "searchable_sources": report.searchable_sources,
        "status_counts": report.status_counts,
        "unsearchable": report.unsearchable,
        "excluded_by_design": report.excluded_by_design,
        "expected_chunks": report.expected_chunks,
        "indexed_chunks": report.indexed_chunks,
        "orphan_chunks": report.orphan_chunks,
        "stale_sources": len(report.stale_sources),
        "embedding_fingerprint": report.embedding_fingerprint,
        "chunker_version": report.chunker_version,
    }


# ----------------------------------------------------------------------------- runs


def _sync_run_payload(row: SyncStatus) -> dict:
    duration = None
    if isinstance(row.started_at, datetime) and isinstance(row.completed_at, datetime):
        duration = (row.completed_at - row.started_at).total_seconds()
    return {
        "id": row.id,
        "job_id": row.job_id,
        # NULL predates the discriminator, and every such row is a circular run.
        "kind": row.kind or "circulars",
        "status": row.status or "unknown",
        "started_at": _iso(row.started_at),
        "completed_at": _iso(row.completed_at),
        "duration_seconds": duration,
        "processed_count": row.processed_count,
        "skipped_count": row.skipped_count,
        "error_count": row.error_count,
        "parameters": row.parameters,
        "error": row.error,
    }


@router.get("/runs")
def run_history(limit: int = Query(25, ge=1, le=200), db: Session = Depends(get_db)) -> dict:
    """Recent sync runs and AI generation jobs, both corpora, newest first.

    Circular and laws syncs share `sync_status` and are told apart by `kind`. They are
    deliberately *not* split here the way the sidebar banner splits them: an operator
    looking at run history wants one timeline of everything that touched the corpus.
    """
    sync_rows = (
        db.query(SyncStatus).order_by(SyncStatus.id.desc()).limit(limit).all()
    )

    job_rows = (
        db.query(AIGenerationJob)
        .order_by(AIGenerationJob.created_at.desc())
        .limit(limit)
        .all()
    )
    # Two lookups rather than a correlated label per job: the target is a circular or a
    # law depending on the discriminator, and a join covering both would be an outer join
    # against two tables for a display string.
    circular_labels = dict(
        db.query(Circular.id, func.coalesce(Circular.reference, Circular.title))
        .filter(Circular.id.in_([j.circular_id for j in job_rows if j.circular_id] or [""]))
        .all()
    )
    law_labels = dict(
        db.query(RegDocument.id, RegDocument.title)
        .filter(RegDocument.id.in_([j.document_id for j in job_rows if j.document_id] or [""]))
        .all()
    )

    return {
        "generated_at": datetime.utcnow().isoformat(),
        "sync_runs": [_sync_run_payload(row) for row in sync_rows],
        "ai_jobs": [
            {
                "id": job.id,
                "target_kind": job.target_kind,
                "target_id": job.circular_id or job.document_id,
                "target_label": (
                    circular_labels.get(job.circular_id)
                    if job.target_kind == "circular"
                    else law_labels.get(job.document_id)
                ),
                "feature": job.feature,
                "status": job.status,
                "result_status": job.result_status,
                "progress_completed": job.progress_completed,
                "progress_total": job.progress_total,
                "created_at": _iso(job.created_at),
                "started_at": _iso(job.started_at),
                "completed_at": _iso(job.completed_at),
                "error": job.error,
            }
            for job in job_rows
        ],
    }


# ---------------------------------------------------------------------- environment


def _tree_usage(path: Path) -> dict:
    """File count and total bytes under `path`, or a reason it could not be measured.

    `os.walk` over a few thousand files is cheap locally and merely slow on a network
    volume; either way a failure here must not take the whole tab down, so it degrades to
    nulls rather than raising.
    """
    if not path.exists():
        return {"path": str(path), "exists": False, "files": 0, "bytes": 0}
    files = 0
    total = 0
    try:
        for root, _dirs, names in os.walk(path):
            for name in names:
                try:
                    total += os.stat(os.path.join(root, name)).st_size
                    files += 1
                except OSError:
                    continue
    except OSError as exc:
        logger.warning("Could not measure %s: %s", path, exc)
        return {"path": str(path), "exists": True, "files": None, "bytes": None}
    return {"path": str(path), "exists": True, "files": files, "bytes": total}


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _docling_available() -> bool:
    """Whether checklist generation can run here at all.

    `docling` is an optional extra and is absent from the deployment image by design
    (invariant 3.11) — it was the sole path to 19 GB of CUDA runtime. Checking the module
    spec rather than importing keeps this from pulling torch into the web process on a
    machine that does have it.
    """
    from importlib.util import find_spec

    try:
        return find_spec("docling") is not None
    except (ImportError, ValueError):
        return False


@router.get("/environment")
def environment(db: Session = Depends(get_db)) -> dict:
    """Where this deployment keeps its data, and what it is able to do.

    The capability flags exist because the same build behaves differently in two places:
    on a maintainer's machine everything works, while on the deployment SBP is
    unreachable and `docling` is absent. Reporting that is what lets the UI say why
    something is unavailable instead of offering a control that always fails.
    """
    chunk_total, store_state = _vector_store_state()
    return {
        "generated_at": datetime.utcnow().isoformat(),
        "data_root": str(DATA_ROOT),
        "databases": [
            {
                "name": "corpus",
                "path": str(database_module.DATA_ROOT / "sbpeye.db"),
                "bytes": _file_size(database_module.DATA_ROOT / "sbpeye.db"),
            },
            {
                "name": "app",
                "path": str(database_module.APP_DATABASE_PATH),
                "bytes": _file_size(database_module.APP_DATABASE_PATH),
            },
            {
                "name": "debug",
                "path": str(database_module.DEBUG_DATABASE_PATH),
                "bytes": _file_size(database_module.DEBUG_DATABASE_PATH),
            },
        ],
        # Ordered by how much it costs to lose the tree, which is the same order `env.py`
        # documents it in — the archive first, because nothing may ever delete from it.
        "file_trees": [
            {"name": "laws archive", "deletable": False, **_tree_usage(LAWS_ARCHIVE_DIR)},
            {"name": "circular files", "deletable": False, **_tree_usage(CIRCULAR_FILES_DIR)},
            {"name": "cache", "deletable": True, **_tree_usage(FILES_CACHE_DIR)},
            {"name": "vector store", "deletable": False, **_tree_usage(database_module.CHROMA_DB_DIR)},
        ],
        "capabilities": {
            "checklist_generation": _docling_available(),
            "llm_debug_allowed": debug_allowed(),
            "vector_store": store_state,
            "vector_store_chunks": chunk_total,
            "ecodata_refresh_seconds": os.getenv("SBPEYE_ECODATA_REFRESH_SECONDS"),
        },
        "embedding": _embedding_section(),
    }
