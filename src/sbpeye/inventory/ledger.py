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
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from ..checklist import prepare_index_chunks
from ..models import SemanticIndexSource
from .corpus import (
    STATUS_INDEXED,
    STATUS_STALE,
    CorpusScope,
    SourceRef,
    build_scope,
)
from .fingerprint import CHUNKER_VERSION, content_hash, embedding_fingerprint

CHUNK_ID_SEPARATOR = "__chunk_"


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


def _ledger_row_id(source: SourceRef) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, source.ledger_id))


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
            expected = len(prepare_index_chunks({
                "doc_id": source.source_id,
                "doc_type": source.source_kind,
                "doc_label": source.label,
                "text": source.text,
                "file_type": "",
            }))
            status = (
                STATUS_INDEXED
                if expected > 0 and indexed_chunks == expected
                else STATUS_STALE
            )
            error = None
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
                id=_ledger_row_id(source),
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
