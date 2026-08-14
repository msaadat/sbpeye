"""Reconcile SQLite, the vector store, and the semantic index ledger.

The ledger answers one question the application could not previously answer: *which
sources are actually searchable, and why is every other one not?* Without it an inventory
result cannot distinguish "no document discusses this" from "the documents that do were
never indexed".

Reconciliation is deliberately one-way — it reads Chroma and the database and writes only
ledger rows. Repairing the index is a separate, explicit action.

See docs/INVENTORY_SEARCH_PLAN.md section 10.
"""

import hashlib
import logging
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from ..checklist import prepare_index_chunks
from ..models import SemanticIndexSource
from .corpus import (
    STATUS_EMPTY,
    STATUS_INDEX_ERROR,
    STATUS_INDEXED,
    STATUS_STALE,
    CorpusScope,
    SourceRef,
    build_scope,
)
from .fingerprint import CHUNKER_VERSION, content_hash, embedding_fingerprint

CHUNK_ID_SEPARATOR = "__chunk_"
PAGE_SIZE = 5000

logger = logging.getLogger(__name__)


@dataclass
class LedgerReport:
    """What reconciliation found. Counts are by ledger status."""

    status_counts: dict[str, int] = field(default_factory=dict)
    excluded_by_design: dict[str, int] = field(default_factory=dict)
    unsearchable: dict[str, int] = field(default_factory=dict)
    expected_chunks: int = 0
    indexed_chunks: int = 0
    orphan_chunks: int = 0
    stale_sources: list[SourceRef] = field(default_factory=list)
    embedding_fingerprint: str = ""
    chunker_version: str = CHUNKER_VERSION

    @property
    def is_complete(self) -> bool:
        """True only when every text-bearing source in scope is currently indexed.

        Extraction failures and unsupported files make this false even though nothing is
        broken — the caller is entitled to know the corpus had holes in it.
        """
        return (
            self.status_counts.get(STATUS_STALE, 0) == 0
            and self.orphan_chunks == 0
            and not self.unsearchable
        )

    @property
    def searchable_sources(self) -> int:
        return self.status_counts.get(STATUS_INDEXED, 0) + self.status_counts.get(
            STATUS_STALE, 0
        )


def chunk_counts_by_source(collection, batch_size: int = 5000) -> Counter:
    """Count stored chunks per source id, in one pass over the collection.

    Chunk ids are ``{source_id}__chunk_{n}`` for all three source kinds, so the prefix
    identifies the source without needing a metadata query per source — 5,000-odd
    filtered Chroma calls would dominate the runtime of an audit.
    """
    counts: Counter = Counter()
    offset = 0
    while True:
        page = collection.get(limit=batch_size, offset=offset, include=[])
        ids = page.get("ids", [])
        if not ids:
            return counts
        for chunk_id in ids:
            source_id = chunk_id.rsplit(CHUNK_ID_SEPARATOR, 1)[0]
            counts[source_id] += 1
        offset += len(ids)


def _ledger_row_id(ledger_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, ledger_id))


def expected_chunk_count(text: str) -> int:
    """How many chunks the canonical chunker produces from this text.

    Chunk count depends only on the text, not the label, so callers do not need to
    reconstruct a document dict to predict it.
    """
    return len(prepare_index_chunks({
        "doc_id": "", "doc_type": "", "doc_label": "", "text": text, "file_type": "",
    }))


def status_for(expected: int, indexed: int) -> tuple[str, str | None]:
    """The ledger status implied by expected versus stored chunk counts.

    One rule, shared by the reconciler and the live indexing paths, so a source cannot
    be described one way by a sync and another way by an audit.
    """
    if expected == 0:
        # Text that survives `.strip()` but chunks to nothing: a scanned file that
        # extracted to page markers and no words. Nothing was lost by the indexer, so
        # this is an empty source, not a stale one — calling it stale would make
        # `--repair` re-embed it forever without effect.
        return STATUS_EMPTY, "source text produced no chunks"
    if indexed == expected:
        return STATUS_INDEXED, None
    return STATUS_STALE, f"expected {expected} chunk(s), found {indexed}"


def record_source(
    db: Session,
    *,
    source_kind: str,
    source_id: str,
    logical_kind: str,
    logical_document_id: str,
    text: str,
    indexed_chunks: int,
    version_id: str | None = None,
    status: str | None = None,
    error: str | None = None,
    embedding_config=None,
) -> None:
    """Upsert one ledger row from an indexing path.

    Called by the same functions that write Chroma and FTS, so the ledger describes the
    index as it is rather than as it was at the last audit. Never raises: a bookkeeping
    failure must not fail an index write, and an absent or stale row makes coverage
    pessimistic, which is the safe direction.
    """
    try:
        if embedding_config is None:
            from ..database import embedding_config as default_config

            embedding_config = default_config

        expected = expected_chunk_count(text)
        if status is None:
            status, error = status_for(expected, indexed_chunks)

        ledger_id = f"{source_kind}:{source_id}"
        row = (
            db.query(SemanticIndexSource)
            .filter_by(source_kind=source_kind, source_id=source_id)
            .one_or_none()
        )
        if row is None:
            row = SemanticIndexSource(
                id=_ledger_row_id(ledger_id),
                source_kind=source_kind,
                source_id=source_id,
            )
            db.add(row)
        row.logical_kind = logical_kind
        row.logical_document_id = logical_document_id
        row.version_id = version_id
        row.content_hash = content_hash(text)
        row.chunker_version = CHUNKER_VERSION
        row.embedding_fingerprint = embedding_fingerprint(embedding_config)
        row.expected_chunks = expected
        row.indexed_chunks = indexed_chunks
        row.status = status
        row.error = error
        row.indexed_at = datetime.utcnow()
        db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("failed to record ledger row for %s:%s", source_kind, source_id)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def reconcile(
    db: Session,
    collection,
    embedding_config,
    scope: CorpusScope | None = None,
    write: bool = True,
) -> LedgerReport:
    """Compare expected sources with stored chunks and refresh the ledger.

    A source counts as ``indexed`` only when the number of chunks in the store equals
    what the canonical chunker produces from its current text. Any drift — edited text,
    a changed chunker, a half-finished write — leaves it ``stale``, which is what makes
    the count meaningful rather than merely optimistic.
    """
    scope = scope if scope is not None else build_scope(db)
    fingerprint = embedding_fingerprint(embedding_config)
    stored_counts = chunk_counts_by_source(collection)
    now = datetime.utcnow()

    report = LedgerReport(
        excluded_by_design=dict(scope.excluded_by_design),
        embedding_fingerprint=fingerprint,
    )
    existing = {
        row.source_kind + ":" + row.source_id: row
        for row in db.query(SemanticIndexSource).all()
    }
    seen_source_ids: set[str] = set()

    for source in scope.sources:
        seen_source_ids.add(source.source_id)
        indexed_chunks = stored_counts.get(source.source_id, 0)

        if source.is_searchable:
            expected = expected_chunk_count(source.text)
            status, error = status_for(expected, indexed_chunks)
            if status == STATUS_EMPTY:
                report.unsearchable[status] = report.unsearchable.get(status, 0) + 1
            report.expected_chunks += expected
        else:
            expected = 0
            status = source.unsearchable_status
            error = source.unsearchable_detail
            report.unsearchable[status] = report.unsearchable.get(status, 0) + 1

        report.indexed_chunks += indexed_chunks
        report.status_counts[status] = report.status_counts.get(status, 0) + 1
        if status == STATUS_STALE:
            report.stale_sources.append(source)

        if not write:
            continue

        row = existing.get(source.ledger_id)
        if row is None:
            row = SemanticIndexSource(
                id=_ledger_row_id(source.ledger_id),
                source_kind=source.source_kind,
                source_id=source.source_id,
            )
            db.add(row)
        row.logical_kind = source.logical_kind
        row.logical_document_id = source.logical_document_id
        row.version_id = source.version_id
        row.content_hash = content_hash(source.text)
        row.chunker_version = CHUNKER_VERSION
        row.embedding_fingerprint = fingerprint
        row.expected_chunks = expected
        row.indexed_chunks = indexed_chunks
        row.status = status
        row.error = error
        row.indexed_at = now

    # Chunks whose source is no longer in scope: a deleted circular, a superseded law
    # version, or a stale write. They must not be scored — an inventory built partly
    # from text the database no longer considers current is not an inventory.
    report.orphan_chunks = sum(
        count
        for source_id, count in stored_counts.items()
        if source_id not in seen_source_ids
    )

    if write:
        stale_rows = [
            row for key, row in existing.items()
            if key.split(":", 1)[1] not in seen_source_ids
        ]
        for row in stale_rows:
            db.delete(row)
        db.commit()

    return report


def indexed_source_ids(db: Session) -> set[str]:
    """Source ids the ledger currently vouches for as fully indexed.

    The scoring layer uses this to decide which stored chunks are allowed to score.
    Anything else in the vector store is an orphan — a deleted circular, a superseded law
    version, a half-finished write — and must not be able to produce a hit.
    """
    return {
        row[0]
        for row in db.query(SemanticIndexSource.source_id)
        .filter(SemanticIndexSource.status == STATUS_INDEXED)
        .all()
    }


def lexical_indexed_documents(
    db: Session, include_circulars: bool = True, include_laws: bool = True
) -> set[tuple[str, str]]:
    """Logical documents that currently have an FTS row.

    Reported separately from the vector arm because the two fail independently: a source
    whose embeddings are stale is still findable lexically, and a caller assessing a
    completeness claim needs to know which arm has the hole.
    """
    from sqlalchemy import text as sql_text

    found: set[tuple[str, str]] = set()
    targets = []
    if include_circulars:
        targets.append(("circular", "circulars_fts", "circular_id"))
    if include_laws:
        targets.append(("law", "laws_fts", "document_id"))
    for logical_kind, table, id_column in targets:
        try:
            rows = db.execute(sql_text(f"SELECT DISTINCT {id_column} FROM {table}")).all()
        except Exception:  # noqa: BLE001 - a missing table is a total gap, not a crash
            logger.exception("could not read %s for coverage", table)
            continue
        found.update((logical_kind, row[0]) for row in rows)
    return found


def recorded_fingerprints(db: Session) -> set[str]:
    """Embedding fingerprints the current index was actually built with."""
    return {
        row[0]
        for row in db.query(SemanticIndexSource.embedding_fingerprint)
        .filter(SemanticIndexSource.status == STATUS_INDEXED)
        .distinct()
        .all()
        if row[0]
    }


def snapshot_id(db: Session) -> str:
    """Hash the active ledger into a corpus identity.

    Changes whenever searchable content, currency, extraction state, chunking, or the
    embedding model changes — which is exactly when a cached embedding matrix or a
    previously issued inventory stops being valid.
    """
    rows = (
        db.query(SemanticIndexSource)
        .order_by(SemanticIndexSource.source_kind, SemanticIndexSource.source_id)
        .all()
    )
    digest = hashlib.sha256()
    for row in rows:
        digest.update(
            "|".join([
                row.source_kind,
                row.source_id,
                row.logical_document_id,
                row.version_id or "",
                row.content_hash or "",
                str(row.expected_chunks),
                row.embedding_fingerprint or "",
                row.chunker_version or "",
                row.status,
            ]).encode("utf-8")
        )
        digest.update(b"\x00")
    return "sha256:" + digest.hexdigest()
