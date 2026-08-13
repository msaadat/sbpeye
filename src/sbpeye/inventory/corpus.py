"""Canonical enumeration of searchable sources and their logical documents.

One place decides what the corpus *is*. Every other module — the ledger writer, the audit
command, the retrieval layer, the coverage report — reads that decision from here, so an
inventory claim can never rest on two different ideas of what should have been indexed.

A **source** is a physical unit of text that gets chunked and embedded: a circular body,
one attachment, or one law version. A **logical document** is what a result row is: a
circular (with its attachments rolled up) or a RegDocument.

See docs/INVENTORY_SEARCH_PLAN.md sections 5.2 and 10.
"""

from dataclasses import dataclass, field
from typing import Iterator, Literal

from sqlalchemy.orm import Session, joinedload

from ..models import Attachment, Circular, RegDocument
from ..search import NON_TEXT_LAW_FILE_TYPES

SourceKind = Literal["circular", "attachment", "law_version"]
LogicalKind = Literal["circular", "law"]

# Why a source carries no searchable text. These are reported in coverage; only
# `extraction_error` and `index_error` represent something that went wrong.
STATUS_INDEXED = "indexed"
STATUS_EMPTY = "empty"
STATUS_UNSUPPORTED = "unsupported"
STATUS_EXTRACTION_ERROR = "extraction_error"
STATUS_INDEX_ERROR = "index_error"
STATUS_STALE = "stale"

# Reasons a row is deliberately outside the corpus rather than a coverage failure.
EXCLUDED_MANIFEST = "law_manifest"
EXCLUDED_CIRCULAR_BACKED = "law_backed_by_circular"
EXCLUDED_EXTERNAL = "law_external"
EXCLUDED_DELISTED = "law_delisted"
EXCLUDED_NO_CURRENT_VERSION = "law_no_current_version"


@dataclass(frozen=True)
class SourceRef:
    """One physical searchable unit, resolved to the document it belongs to."""

    source_kind: SourceKind
    source_id: str
    logical_kind: LogicalKind
    logical_document_id: str
    version_id: str | None = None
    label: str = ""
    text: str = ""
    # Set when the source cannot yield chunks; `text` is then empty.
    unsearchable_status: str | None = None
    unsearchable_detail: str | None = None

    @property
    def is_searchable(self) -> bool:
        return self.unsearchable_status is None and bool(self.text.strip())

    @property
    def ledger_id(self) -> str:
        return f"{self.source_kind}:{self.source_id}"


@dataclass
class CorpusScope:
    """What a given request considers in scope, and what it deliberately left out."""

    sources: list[SourceRef] = field(default_factory=list)
    excluded_by_design: dict[str, int] = field(default_factory=dict)

    def exclude(self, reason: str) -> None:
        self.excluded_by_design[reason] = self.excluded_by_design.get(reason, 0) + 1

    @property
    def searchable(self) -> list[SourceRef]:
        return [source for source in self.sources if source.is_searchable]

    @property
    def unsearchable(self) -> list[SourceRef]:
        return [source for source in self.sources if not source.is_searchable]

    def logical_documents(self) -> set[tuple[str, str]]:
        return {(s.logical_kind, s.logical_document_id) for s in self.sources}


def _circular_body_source(circular: Circular) -> SourceRef:
    """The circular's own HTML body.

    Text is read straight from the column rather than through
    ``scraper.circulars.circular_document``: that helper re-extracts from the HTML cache
    when a file happens to be present, which would make the ledger's content hash depend
    on whether a cache file exists on this machine.
    """
    text = circular.content_text or ""
    status = None if text.strip() else STATUS_EMPTY
    return SourceRef(
        source_kind="circular",
        source_id=circular.id,
        logical_kind="circular",
        logical_document_id=circular.id,
        label=circular.reference or circular.title or circular.id,
        text=text,
        unsearchable_status=status,
    )


def _attachment_source(attachment: Attachment) -> SourceRef:
    text = attachment.content_text or ""
    status = None
    detail = None
    if not text.strip():
        extraction = (attachment.extraction_status or "").lower()
        if extraction in {"error", "failed"}:
            status = STATUS_EXTRACTION_ERROR
            detail = attachment.extraction_error
        elif extraction == "unsupported":
            status = STATUS_UNSUPPORTED
        else:
            status = STATUS_EMPTY
    return SourceRef(
        source_kind="attachment",
        source_id=attachment.id,
        logical_kind="circular",
        logical_document_id=attachment.circular_id,
        label=attachment.filename,
        text=text,
        unsearchable_status=status,
        unsearchable_detail=detail,
    )


def iter_circular_sources(
    db: Session, circular_ids: set[str] | None = None
) -> Iterator[SourceRef]:
    """Every circular body and attachment, in stable ID order."""
    query = db.query(Circular).options(joinedload(Circular.attachments))
    if circular_ids is not None:
        if not circular_ids:
            return
        query = query.filter(Circular.id.in_(circular_ids))

    for circular in query.order_by(Circular.id).all():
        yield _circular_body_source(circular)
        for attachment in sorted(circular.attachments, key=lambda a: a.id):
            yield _attachment_source(attachment)


def iter_law_sources(
    db: Session,
    scope: CorpusScope | None = None,
    document_ids: set[str] | None = None,
    include_delisted: bool = False,
) -> Iterator[SourceRef]:
    """Every current, text-bearing law version, in stable ID order.

    Applies the same currency and manifest rules as ``laws.index_pending_laws`` — a
    document is searchable through the version in force, and only that one. Rows outside
    the corpus by design are counted on ``scope`` rather than reported as gaps.
    """
    query = db.query(RegDocument)
    if document_ids is not None:
        if not document_ids:
            return
        query = query.filter(RegDocument.id.in_(document_ids))

    for document in query.order_by(RegDocument.id).all():
        if document.circular_id:
            # The listing row is a circular already in the circular corpus; indexing it
            # again would return the same instrument as two results.
            if scope:
                scope.exclude(EXCLUDED_CIRCULAR_BACKED)
            continue
        if document.is_external:
            if scope:
                scope.exclude(EXCLUDED_EXTERNAL)
            continue
        if document.delisted_at is not None and not include_delisted:
            if scope:
                scope.exclude(EXCLUDED_DELISTED)
            continue

        version = document.current_version
        if version is None:
            if scope:
                scope.exclude(EXCLUDED_NO_CURRENT_VERSION)
            continue
        if version.file_type in NON_TEXT_LAW_FILE_TYPES:
            # A container manifest is bookkeeping, not text. Its parts are separate
            # RegDocument rows and are enumerated in their own right.
            if scope:
                scope.exclude(EXCLUDED_MANIFEST)
            continue

        text = version.content_text or ""
        status = None
        detail = None
        if not text.strip():
            extraction = (version.extraction_status or "").lower()
            if extraction in {"error", "failed"}:
                status = STATUS_EXTRACTION_ERROR
                detail = version.extraction_error
            else:
                status = STATUS_EMPTY

        label = document.title or document.id
        if document.part_label and document.parent is not None:
            label = f"{document.parent.title} - {document.part_label}: {label}"

        yield SourceRef(
            source_kind="law_version",
            source_id=version.id,
            logical_kind="law",
            logical_document_id=document.id,
            version_id=version.id,
            label=label,
            text=text,
            unsearchable_status=status,
            unsearchable_detail=detail,
        )


def build_scope(
    db: Session,
    include_circulars: bool = True,
    include_laws: bool = True,
    circular_ids: set[str] | None = None,
    law_document_ids: set[str] | None = None,
    include_delisted_laws: bool = False,
) -> CorpusScope:
    """Enumerate every source the request should have searched."""
    scope = CorpusScope()
    if include_circulars:
        scope.sources.extend(iter_circular_sources(db, circular_ids))
    if include_laws:
        scope.sources.extend(
            iter_law_sources(
                db,
                scope=scope,
                document_ids=law_document_ids,
                include_delisted=include_delisted_laws,
            )
        )
    return scope
