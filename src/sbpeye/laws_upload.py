"""Admin-uploaded documents in the laws corpus (docs/LAWS_UPLOADS_PLAN.md).

The motivating case is an Act SBP lists but does not host — the Banking Companies
Ordinance 1962 lives on pakistancode.gov.pk — plus relevant laws SBP does not list at
all. Both arrive here as bytes an administrator supplies.

The design principle that shapes every function below: **an upload is a version, not a
new kind of document**. There is no upload table. A `RegDocument` whose text arrived by
upload is the same row as one whose text arrived by download, identified the same way
(normalized title), archived in the same tree, indexed by the same pass, and read by
every consumer through `current_version` as before. What uploads add is provenance —
`RegDocument.origin`, `RegDocumentVersion.source = "upload"` and the columns beside it —
not structure.

So a collision with an existing document is the feature, not an error: uploading the text
of the Banking Companies Ordinance attaches it to the listing row SBP already gave us,
with its 314 backlinks and its place in the name index, because
`law_identity("Banking Companies Ordinance, 1962")` is the id that row already has.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from .env import LAWS_ARCHIVE_DIR, PROJECT_ROOT
from .laws_links import (
    build_part_index,
    identifying_names,
    link_circular_to_laws,
    normalize_link,
)
from .models import Circular, RegDocument, RegDocumentVersion
from .scraper.circulars import _content_matches_file_type, extract_document_text
from .scraper.laws import (
    index_law_document,
    law_identity,
    normalize_law_title,
    select_current_versions,
)

# What an admin may upload today. Deliberately short: the test is not "can a browser
# display it" but "can the analysis pipeline read it". `html` needs a cleaner that is not
# SBP-specific and `docx` a dependency the deployment image does not carry, so both are
# out of v1 (LAWS_UPLOADS_PLAN.md §4, §8).
ACCEPTED_FILE_TYPES = ("pdf", "txt")

# Doc types an upload may claim, matching the values SBP's own listing produces so the
# type facet needs no special case for uploads.
ACCEPTED_DOC_TYPES = ("law", "regulation", "guideline", "gazette", "licensing")


class UploadRejected(Exception):
    """The upload cannot be stored, with a reason meant for the admin to read."""


@dataclass
class UploadTarget:
    """Where an upload would land, resolved before anything is written.

    The form shows this as a preview so an admin commits to an attachment rather than
    discovering it: "new document", or "attaches to *Banking Companies Ordinance, 1962*
    (listed by SBP, hosted externally, no text held)".
    """

    document_id: str
    title: str
    exists: bool
    # Populated only when `exists`; the state an admin needs to judge the attachment.
    doc_type: str | None = None
    origin: str | None = None
    is_external: bool = False
    is_delisted: bool = False
    held_versions: int = 0
    holds_text: bool = False
    is_circular_backed: bool = False

    @property
    def summary(self) -> str:
        """One line describing the target, for the form and the CLI."""
        if not self.exists:
            return "New document."
        # `origin` is NULL on rows written before the column existed, and those are all
        # listing rows — the same reasoning as `models.circular_sync_only`.
        parts = ["Uploaded document" if self.origin == "upload" else "Listed by SBP"]
        if self.is_external:
            parts.append("hosted externally")
        if self.is_circular_backed:
            parts.append("resolves to a circular")
        if self.is_delisted:
            parts.append("delisted")
        parts.append(
            f"{self.held_versions} edition(s) held"
            if self.holds_text
            else "no text held"
        )
        return ", ".join(parts) + "."


@dataclass
class UploadResult:
    """What actually happened, in the terms the response has to report.

    `will_be_searchable` and `is_current` are here because of principle 5 of the plan:
    extraction runs synchronously so an upload of a scanned PDF can say "no text layer,
    this will not be searchable" at upload time, rather than leaving it to be discovered
    weeks later by someone who searched and found nothing.
    """

    document: RegDocument
    version: RegDocumentVersion
    created_document: bool
    created_version: bool
    extraction_status: str
    will_be_searchable: bool
    is_current: bool
    # Set when these exact bytes were already held. The same bytes are the same edition,
    # so nothing is stored and the existing version is handed back instead.
    duplicate_of: RegDocumentVersion | None = None


# ------------------------------------------------------------------- identity


def resolve_upload_target(
    db: Session, title: str, document_id: str | None = None
) -> UploadTarget:
    """Which document this title would attach to, without writing anything.

    An explicit `document_id` overrides title resolution, for the case where the admin's
    wording differs from SBP's ("Banking Companies Ordinance 1962" against SBP's
    "Banking Companies Ordinance, 1962"). Otherwise identity is the normalized title,
    exactly as for a listing row.
    """
    title = (title or "").strip()
    if not title:
        raise UploadRejected("A title is required.")

    resolved_id = document_id or law_identity(title)
    document = db.query(RegDocument).filter(RegDocument.id == resolved_id).first()
    if document is None:
        if document_id:
            # An explicit id is a claim about a document that exists. Silently creating a
            # row under it would produce a document whose id encodes nothing.
            raise UploadRejected(f"No document with id {document_id}.")
        return UploadTarget(document_id=resolved_id, title=title, exists=False)

    held = [v for v in document.versions]
    return UploadTarget(
        document_id=document.id,
        title=document.title or title,
        exists=True,
        doc_type=document.doc_type,
        origin=document.origin,
        is_external=bool(document.is_external),
        is_delisted=document.delisted_at is not None,
        held_versions=len(held),
        holds_text=any((v.content_text or "").strip() for v in held),
        is_circular_backed=document.circular_id is not None,
    )


# ------------------------------------------------------------------- archive


def _upload_archive_name(content_hash: str, filename: str) -> str:
    """`<hash8>-<original filename>`, matching what a downloaded version is archived as.

    One naming convention across the tree means the archive reads the same however the
    bytes arrived, and the hash prefix keeps two editions of one document apart.
    """
    safe = Path(filename or "document").name.strip() or "document"
    # Path separators are already gone; these are the characters that make an archive
    # file awkward to handle from a shell or a URL rather than unsafe.
    for character in '\\:*?"<>|':
        safe = safe.replace(character, "_")
    return f"{content_hash[:8]}-{safe}"


def _file_type_of(filename: str) -> str:
    return Path(filename or "").suffix.lstrip(".").lower()


def _extract_upload_text(
    path: Path, file_type: str
) -> tuple[str | None, str, str | None]:
    """Text for an uploaded file, as (text, status, error).

    `txt` is the one type that needs no extractor — the bytes *are* the text — and it is
    handled here rather than in `extract_document_text` because a plain-text upload is
    the only place in either corpus where the question comes up.
    """
    if file_type == "txt":
        try:
            return path.read_text(encoding="utf-8"), "extracted", None
        except UnicodeDecodeError as exc:
            # Saying so beats storing replacement characters as if they were the law.
            return None, "error", f"Not valid UTF-8 text: {exc}"
    return extract_document_text(path, file_type)


# ------------------------------------------------------------------- the upload


def store_upload(
    db: Session,
    *,
    path_or_bytes: Path | str | bytes,
    filename: str,
    title: str,
    doc_type: str = "law",
    source_url: str | None = None,
    source_note: str | None = None,
    version_label: str | None = None,
    effective_from: datetime | None = None,
    uploaded_by: str | None = None,
    document_id: str | None = None,
    pin: bool = False,
    now: datetime | None = None,
) -> UploadResult:
    """Put one uploaded file into the corpus as a version of its document.

    Extraction runs here, synchronously, so the caller can report searchability at upload
    time. Indexing and backlinking do not — they are `finish_upload`, which the API runs
    in a background thread and the CLI runs inline.
    """
    now = now or datetime.utcnow()
    file_type = _file_type_of(filename)
    if file_type not in ACCEPTED_FILE_TYPES:
        raise UploadRejected(
            f"{file_type or 'This file type'} cannot be uploaded. "
            f"Accepted types: {', '.join(ACCEPTED_FILE_TYPES)}. "
            "A spreadsheet or Word file would be stored but unreadable, which is worse "
            "than being refused."
        )
    if doc_type not in ACCEPTED_DOC_TYPES:
        raise UploadRejected(
            f"Unknown document type {doc_type!r}. "
            f"Expected one of: {', '.join(ACCEPTED_DOC_TYPES)}."
        )

    payload = (
        Path(path_or_bytes).read_bytes()
        if isinstance(path_or_bytes, (str, Path))
        else path_or_bytes
    )
    if not payload:
        raise UploadRejected("The file is empty.")
    if not _content_matches_file_type(payload[:1024], file_type):
        # The same sniff that catches SBP serving a dead link as 200-with-HTML catches a
        # renamed file here.
        raise UploadRejected(f"This file's contents are not a {file_type}.")

    target = resolve_upload_target(db, title, document_id)
    content_hash = hashlib.sha256(payload).hexdigest()

    # Dedupe across *every* document, not just this one. The same bytes are the same
    # edition wherever they were uploaded, and storing them twice would fork one
    # document's history into two.
    duplicate = (
        db.query(RegDocumentVersion)
        .filter(RegDocumentVersion.content_hash == content_hash)
        .first()
    )
    if duplicate is not None:
        return UploadResult(
            document=duplicate.document,
            version=duplicate,
            created_document=False,
            created_version=False,
            extraction_status=duplicate.extraction_status,
            will_be_searchable=bool((duplicate.content_text or "").strip()),
            is_current=bool(duplicate.is_current),
            duplicate_of=duplicate,
        )

    # Archive before the database, so a row can never point at bytes that are not there.
    # The reverse order fails the other way: a crash between the two leaves an archive
    # file no row references, which costs disk and nothing else — and a later upload of
    # the same bytes lands on the same path and adopts it.
    archive_dir = LAWS_ARCHIVE_DIR / target.document_id
    archive_dir.mkdir(parents=True, exist_ok=True)
    destination = archive_dir / _upload_archive_name(content_hash, filename)
    if not destination.exists():
        # Written via a temp file in the same directory so a partial write is never
        # visible under the final name. Nothing in the archive is ever overwritten.
        temp_path = archive_dir / f".part-{uuid.uuid4().hex}"
        try:
            temp_path.write_bytes(payload)
            temp_path.replace(destination)
        finally:
            temp_path.unlink(missing_ok=True)

    document = db.query(RegDocument).filter(RegDocument.id == target.document_id).first()
    created_document = document is None
    if document is None:
        document = RegDocument(
            id=target.document_id,
            title=title.strip(),
            normalized_title=normalize_law_title(title),
            doc_type=doc_type,
            # An upload-origin row is ours, not SBP's: `is_external` says "SBP hosts this
            # elsewhere", which is a claim about SBP's listing and not ours to make.
            is_external=0,
            origin="upload",
            source_url=source_url or None,
            first_seen_at=now,
            last_seen_at=now,
        )
        db.add(document)
    else:
        # An existing row keeps its origin, title, type and external flag — they came
        # from SBP's listing and the upload is not evidence against any of them.
        document.last_seen_at = now
    if source_note:
        document.source_note = source_note

    version = RegDocumentVersion(
        id=str(
            uuid.uuid5(
                uuid.NAMESPACE_URL, f"sbp-law-version:{document.id}:{content_hash}"
            )
        ),
        document_id=document.id,
        content_hash=content_hash,
        # Null on purpose, and load-bearing: it is what tells
        # `main._ensure_law_version_cached` there is nothing to re-fetch. An upload's
        # bytes are the least reproducible in the system.
        file_url=None,
        local_path=destination.relative_to(PROJECT_ROOT).as_posix(),
        file_type=file_type,
        version_label=version_label or None,
        effective_from=effective_from,
        source="upload",
        pinned=1 if pin else 0,
        uploaded_by=uploaded_by,
        original_filename=Path(filename).name,
        # Currency is decided below by the one function allowed to decide it.
        is_current=0,
        is_vectorized=0,
        first_seen_at=now,
        last_seen_at=now,
    )
    text, status, error = _extract_upload_text(destination, file_type)
    version.content_text = text
    version.extraction_status = status
    version.extraction_error = error
    db.add(version)
    db.flush()

    if pin:
        _clear_other_pins(db, document.id, version.id)
    select_current_versions(db, {document.id}, now=now)
    db.commit()

    return UploadResult(
        document=document,
        version=version,
        created_document=created_document,
        created_version=True,
        extraction_status=status,
        will_be_searchable=bool((text or "").strip()),
        is_current=bool(version.is_current),
    )


def finish_upload(db: Session, document_id: str, verbose: bool = False) -> dict:
    """The deferred half: index the document, then look for circulars that name it.

    The backlink pass is scoped to this one document on purpose.
    `backlink_circulars(rescan=True)` would rescan every circular against every name in
    the corpus to discover links to one new row — the same answer for four orders of
    magnitude more work. No LLM runs here; typing the law→law edges is what the reader's
    existing Generate button is for.
    """
    document = db.query(RegDocument).filter(RegDocument.id == document_id).first()
    if document is None:
        raise UploadRejected(f"No document with id {document_id}.")

    index_law_document(db, document, verbose=verbose)
    db.commit()
    linked = _backlink_one_document(db, document, verbose=verbose)
    return {"indexed": 1, "linked": linked}


def _backlink_one_document(
    db: Session, document: RegDocument, verbose: bool = False
) -> int:
    """Link circulars that name or point at exactly this document. Returns the count.

    Built by handing `link_circular_to_laws` indexes containing only this document, so
    every link it finds is about this document and existing links are untouched.
    """
    if document.parent_id is not None:
        # Only top-level documents are matched by name; a part is reached through its
        # container (see laws_links.build_name_index).
        return 0

    name_index = sorted(
        (
            (name, document.id)
            for name in identifying_names(document.normalized_title or document.title)
        ),
        key=lambda item: -len(item[0]),
    )
    url_index = {}
    for url in [document.source_url] + [v.file_url for v in document.versions]:
        key = normalize_link(url)
        if key:
            url_index[key] = document.id
    if not name_index and not url_index:
        return 0

    # A container's part index is keyed by the container's name, so a scoped pass needs
    # only this document's own entry — absent for a flat document, which uploads are.
    part_index = {
        name: parts
        for name, parts in build_part_index(db).items()
        if document.id in parts.values()
    }

    linked = 0
    for index, circular in enumerate(db.query(Circular).all(), start=1):
        counts = link_circular_to_laws(
            db, circular, url_index, name_index, part_index, verbose=verbose
        )
        if counts["url_scan"] or counts["name_match"]:
            linked += 1
        if index % 500 == 0:
            db.commit()
    db.commit()
    if verbose:
        print(f"  [LINK] {linked} circular(s) reference {document.title[:50]}")
    return linked


# ------------------------------------------------------------------- withdrawal


def withdraw(db: Session, document_id: str, now: datetime | None = None) -> RegDocument:
    """Take an uploaded document out of the corpus without deleting anything.

    `delisted_at` is the mechanism because every reader already honours it — the list,
    both search arms, chat and the analysis gate — and because it is reversible. Nothing
    under `files/laws/` is ever deleted (plan §3.3, and the archive invariant in
    `env.py`).
    """
    document = _require_document(db, document_id)
    document.delisted_at = now or datetime.utcnow()
    index_law_document(db, document)
    db.commit()
    return document


def restore(db: Session, document_id: str) -> RegDocument:
    """Undo `withdraw`."""
    document = _require_document(db, document_id)
    document.delisted_at = None
    index_law_document(db, document)
    db.commit()
    return document


# ------------------------------------------------------------------- pinning


def pin_version(
    db: Session, version_id: str, document_id: str | None = None
) -> RegDocumentVersion:
    """Make this version the edition in force, over SBP's own copy if there is one.

    The case this exists for: SBP hosts the Banks Nationalization Act as a scanned PDF
    with no text layer, so their copy is in force and unsearchable while an uploaded
    consolidated text sits beside it doing nothing. Pinning swaps them.
    """
    version = _require_version(db, version_id, document_id)
    _clear_other_pins(db, version.document_id, version.id)
    version.pinned = 1
    db.flush()
    select_current_versions(db, {version.document_id})
    index_law_document(db, version.document)
    db.commit()
    return version


def unpin_version(
    db: Session, version_id: str, document_id: str | None = None
) -> RegDocumentVersion:
    """Hand currency back to the tier rule — SBP's copy, where there is one."""
    version = _require_version(db, version_id, document_id)
    version.pinned = 0
    db.flush()
    select_current_versions(db, {version.document_id})
    index_law_document(db, version.document)
    db.commit()
    return version


def _clear_other_pins(db: Session, document_id: str, keep_version_id: str) -> None:
    """At most one pin per document — the tier has room for exactly one winner."""
    db.query(RegDocumentVersion).filter(
        RegDocumentVersion.document_id == document_id,
        RegDocumentVersion.id != keep_version_id,
        RegDocumentVersion.pinned == 1,
    ).update({"pinned": 0}, synchronize_session="fetch")


# ------------------------------------------------------------------- listing


def upload_targets(db: Session) -> list[RegDocument]:
    """Documents the Library tab manages: uploads, plus the rows worth uploading to.

    The second group — listed by SBP, hosted off-site, no text held — is the reason the
    tab exists, so it is included rather than left to be hunted for in the main list.
    """
    documents = (
        db.query(RegDocument)
        .filter(RegDocument.parent_id.is_(None))
        .order_by(RegDocument.title)
        .all()
    )
    return [
        document
        for document in documents
        if document.origin == "upload"
        or any(v.source == "upload" for v in document.versions)
        or (document.is_external and not document.versions)
    ]


def _require_document(db: Session, document_id: str) -> RegDocument:
    document = db.query(RegDocument).filter(RegDocument.id == document_id).first()
    if document is None:
        raise UploadRejected(f"No document with id {document_id}.")
    return document


def _require_version(
    db: Session, version_id: str, document_id: str | None = None
) -> RegDocumentVersion:
    """The version, optionally checked against the document the caller thinks owns it.

    The API addresses a version as `/api/laws/{document_id}/versions/{version_id}`. Since
    a version id is unique on its own, ignoring the first half would let a wrong or stale
    document id pin an edition of some *other* document and hand back that document's
    detail — a URL that lies about what it just changed.
    """
    version = (
        db.query(RegDocumentVersion)
        .filter(RegDocumentVersion.id == version_id)
        .first()
    )
    if version is None:
        raise UploadRejected(f"No version with id {version_id}.")
    if document_id is not None and version.document_id != document_id:
        raise UploadRejected(
            f"Version {version_id} belongs to document {version.document_id}, "
            f"not {document_id}."
        )
    return version
