"""Phase 2 of the laws uploads plan: the upload service and its CLI.

No network and no embedding backend — the Chroma write is replaced by a recorder so the
paired FTS/vector writes can be asserted, and PDF extraction is stubbed except where the
point of the test is what extraction reported.

See docs/LAWS_UPLOADS_PLAN.md.
"""

import hashlib
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from sbpeye import laws_upload
from sbpeye.laws_upload import (
    UploadRejected,
    finish_upload,
    pin_version,
    resolve_upload_target,
    restore,
    store_upload,
    unpin_version,
    upload_targets,
    withdraw,
)
from sbpeye.models import Circular, RegDocument, RegDocumentVersion
from sbpeye.scraper import laws

from test_reg_models import make_session

PDF_BYTES = b"%PDF-1.7\nAn Act to consolidate the law relating to banking companies.\n"


@pytest.fixture
def store(monkeypatch, tmp_path):
    """The archive redirected into tmp_path, with the vector store recorded not written."""
    archive = tmp_path / "files" / "laws"
    monkeypatch.setattr(laws_upload, "LAWS_ARCHIVE_DIR", archive)
    monkeypatch.setattr(laws_upload, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(laws, "PROJECT_ROOT", tmp_path)

    chunks: dict[str, int] = {}

    def fake_replace(document, *, metadata_for, delete_kwargs):
        document_id = delete_kwargs["law_document_id"]
        count = len([line for line in (document.get("text") or "").splitlines() if line.strip()])
        if count:
            chunks[document_id] = count
        else:
            chunks.pop(document_id, None)
        return count

    monkeypatch.setattr(laws, "_replace_document_chunks", fake_replace)

    class Store:
        def __init__(self):
            self.root = tmp_path
            self.archive_dir = archive
            self.chunks = chunks

    return Store()


@pytest.fixture
def extracted(monkeypatch):
    """PDF extraction that succeeds, for the tests not about extraction."""
    monkeypatch.setattr(
        laws_upload, "extract_document_text",
        lambda path, file_type: (path.read_bytes().decode("latin-1"), "extracted", None),
    )


def fts_rows(db, document_id: str) -> int:
    return db.execute(
        text("SELECT count(*) FROM laws_fts WHERE document_id = :did"),
        {"did": document_id},
    ).scalar()


def upload(db, **overrides):
    fields = dict(
        path_or_bytes=PDF_BYTES,
        filename="bco-1962.pdf",
        title="Banking Companies Ordinance, 1962",
        doc_type="law",
    )
    fields.update(overrides)
    return store_upload(db, **fields)


# ------------------------------------------------------------------ identity


def test_an_upload_attaches_to_the_external_row_sbp_already_gave_us(store, extracted):
    """The motivating case. Identity does the attaching, so nothing has to be wired up."""
    db = make_session()
    listed = RegDocument(
        id=laws.law_identity("Banking Companies Ordinance, 1962"),
        title="Banking Companies Ordinance, 1962",
        normalized_title=laws.normalize_law_title("Banking Companies Ordinance, 1962"),
        doc_type="law",
        is_external=1,
        source_url="https://pakistancode.gov.pk/english/UY2FqaJw1-apaUY2Fqa-apaUY2Nl-sg-jjjjjjjjjjjjj",
    )
    db.add(listed)
    db.commit()

    target = resolve_upload_target(db, "Banking Companies Ordinance, 1962")
    assert target.exists
    assert target.document_id == listed.id
    assert "hosted externally" in target.summary
    assert "no text held" in target.summary

    result = upload(db)

    assert not result.created_document
    assert result.document.id == listed.id
    # Tier 2 is the only tier here, so the upload is in force with no pin.
    assert result.is_current
    assert result.will_be_searchable
    # The flag is untouched: SBP does still host it elsewhere.
    assert result.document.is_external == 1
    assert result.document.origin == "sbp_listing"


def test_a_title_sbp_words_differently_is_reached_by_explicit_id(store, extracted):
    db = make_session()
    listed = RegDocument(
        id="sbp-row-1",
        title="Banking Companies Ordinance, 1962",
        normalized_title="banking companies ordinance, 1962",
        doc_type="law",
        is_external=1,
    )
    db.add(listed)
    db.commit()

    # Without the id this resolves to a *new* document: the comma is part of the name.
    assert not resolve_upload_target(db, "Banking Companies Ordinance 1962").exists

    result = upload(db, title="Banking Companies Ordinance 1962", document_id="sbp-row-1")

    assert result.document.id == "sbp-row-1"
    assert not result.created_document
    # The listing's wording stands; the admin's was only a way of finding the row.
    assert result.document.title == "Banking Companies Ordinance, 1962"


def test_an_explicit_id_for_no_document_is_refused_rather_than_invented(store):
    db = make_session()
    with pytest.raises(UploadRejected, match="No document with id"):
        resolve_upload_target(db, "Some Act, 2020", document_id="nope")


def test_an_unlisted_law_becomes_its_own_document(store, extracted):
    db = make_session()

    result = upload(db, title="Companies Act, 2017", filename="companies-act.pdf",
                    source_note="Consolidated text from the gazette, March 2024")

    assert result.created_document
    assert result.document.origin == "upload"
    # Ours, not SBP's: `is_external` is a claim about SBP's listing.
    assert result.document.is_external == 0
    assert result.document.source_note == "Consolidated text from the gazette, March 2024"
    assert result.is_current


# ------------------------------------------------------------------ dedupe


def test_the_same_bytes_are_the_same_edition_even_under_another_title(store, extracted):
    """Storing them twice would fork one document's history into two."""
    db = make_session()
    first = upload(db)

    again = upload(db, title="Banking Companies Ordinance 1962 (consolidated)",
                   filename="bco-copy.pdf")

    assert again.duplicate_of is not None
    assert again.version.id == first.version.id
    assert again.document.id == first.document.id
    assert not again.created_version
    assert db.query(RegDocumentVersion).count() == 1
    assert db.query(RegDocument).count() == 1


def test_a_second_edition_supersedes_the_first_and_both_stay_in_the_timeline(store, extracted):
    db = make_session()
    first = upload(db, title="Companies Act, 2017")
    second = upload(
        db, title="Companies Act, 2017",
        path_or_bytes=PDF_BYTES + b"Amended 2024.\n", filename="companies-act-2024.pdf",
    )

    assert second.document.id == first.document.id
    assert second.is_current
    db.refresh(first.version)
    assert first.version.is_current == 0
    assert db.query(RegDocumentVersion).count() == 2


# ------------------------------------------------------------------ file types


def test_a_word_file_is_refused_rather_than_stored_unreadable(store):
    db = make_session()
    with pytest.raises(UploadRejected, match="docx cannot be uploaded"):
        upload(db, path_or_bytes=b"PK\x03\x04stuff", filename="act.docx")


def test_a_renamed_file_is_caught_by_its_bytes(store):
    """The same sniff that catches SBP serving a dead link as 200-with-HTML."""
    db = make_session()
    with pytest.raises(UploadRejected, match="not a pdf"):
        upload(db, path_or_bytes=b"<html>Not found</html>", filename="act.pdf")


def test_an_empty_file_is_refused(store):
    db = make_session()
    with pytest.raises(UploadRejected, match="empty"):
        upload(db, path_or_bytes=b"", filename="act.pdf")


def test_a_text_upload_needs_no_extractor_because_the_bytes_are_the_text(store):
    db = make_session()

    result = upload(
        db, title="Negotiable Instruments Act, 1881",
        path_or_bytes="Section 1. Short title.\nSection 2. Repeal.\n".encode("utf-8"),
        filename="nia-1881.txt",
    )

    assert result.extraction_status == "extracted"
    assert result.will_be_searchable
    assert "Short title" in result.version.content_text
    assert result.version.file_type == "txt"


def test_a_text_upload_that_is_not_utf8_says_so_instead_of_storing_mojibake(store):
    db = make_session()

    result = upload(
        db, title="Gazette Notification, 1999",
        path_or_bytes=b"Section 1. \xff\xfe not text", filename="gazette.txt",
    )

    assert result.extraction_status == "error"
    assert not result.will_be_searchable
    assert "UTF-8" in result.version.extraction_error


def test_a_scanned_pdf_says_at_upload_time_that_it_will_not_be_searchable(store, monkeypatch):
    """Principle 5: found now, not weeks later by someone whose search came back empty."""
    db = make_session()
    monkeypatch.setattr(
        laws_upload, "extract_document_text",
        lambda path, file_type: (None, "scanned", None),
    )

    result = upload(db)

    assert result.extraction_status == "scanned"
    assert not result.will_be_searchable
    # Still archived and still the edition in force — we hold the document, not its text.
    assert result.is_current
    assert (store.root / result.version.local_path).is_file()


# ------------------------------------------------------------------ archive


def test_the_file_lands_in_the_archive_under_its_hash_and_original_name(store, extracted):
    db = make_session()

    result = upload(db)

    archived = store.root / result.version.local_path
    assert archived.is_file()
    assert archived.read_bytes() == PDF_BYTES
    digest = hashlib.sha256(PDF_BYTES).hexdigest()
    assert archived.name == f"{digest[:8]}-bco-1962.pdf"
    assert archived.parent.name == result.document.id
    # There is no URL to retry, and that is what tells the reader not to offer one.
    assert result.version.file_url is None
    assert result.version.original_filename == "bco-1962.pdf"


def test_an_archived_file_is_never_overwritten(store, extracted, tmp_path):
    db = make_session()
    result = upload(db)
    archived = store.root / result.version.local_path

    # Same document, same bytes on disk, but the row is gone — the state a crash between
    # the archive write and the commit leaves behind.
    db.query(RegDocumentVersion).delete()
    db.commit()
    archived.write_bytes(PDF_BYTES)
    before = archived.stat().st_mtime_ns

    again = upload(db)

    assert (store.root / again.version.local_path) == archived
    assert archived.stat().st_mtime_ns == before


# ------------------------------------------------------------------ currency


def test_an_upload_beside_sbps_copy_waits_for_a_pin(store, extracted):
    """The scanned-PDF case, end to end through the service."""
    db = make_session()
    document = RegDocument(
        id=laws.law_identity("Banks Nationalization Act 1974"),
        title="Banks Nationalization Act 1974",
        normalized_title=laws.normalize_law_title("Banks Nationalization Act 1974"),
        doc_type="law",
    )
    db.add(document)
    db.add(RegDocumentVersion(
        id="sbp-scanned", document_id=document.id, content_hash="sbp-hash",
        file_type="pdf", source="live", is_current=1,
        extraction_status="scanned", first_seen_at=datetime(2024, 1, 1),
    ))
    db.commit()

    result = upload(db, title="Banks Nationalization Act 1974", filename="bna-1974.pdf")

    assert not result.is_current
    assert laws_upload._require_version(db, "sbp-scanned").is_current == 1

    pin_version(db, result.version.id)
    assert laws_upload._require_version(db, result.version.id).is_current == 1
    assert laws_upload._require_version(db, "sbp-scanned").is_current == 0

    unpin_version(db, result.version.id)
    assert laws_upload._require_version(db, "sbp-scanned").is_current == 1


def test_only_one_version_of_a_document_can_be_pinned(store, extracted):
    db = make_session()
    first = upload(db, title="Companies Act, 2017", pin=True)
    second = upload(
        db, title="Companies Act, 2017", pin=True,
        path_or_bytes=PDF_BYTES + b"Amended.\n", filename="companies-act-2024.pdf",
    )

    db.refresh(first.version)
    assert first.version.pinned == 0
    assert second.version.pinned == 1
    assert (
        db.query(RegDocumentVersion).filter(RegDocumentVersion.pinned == 1).count() == 1
    )


# ------------------------------------------------------------------ indexing


def test_finish_upload_indexes_both_arms_and_withdrawal_takes_both_back(store, extracted):
    """Every text-mutating path pairs its FTS write with its Chroma write (§3.6)."""
    db = make_session()
    result = upload(db)
    document_id = result.document.id

    finish_upload(db, document_id)

    assert fts_rows(db, document_id) == 1
    assert store.chunks.get(document_id)

    withdraw(db, document_id)

    assert fts_rows(db, document_id) == 0
    assert document_id not in store.chunks
    # Withdrawal is reversible and deletes nothing.
    assert (store.root / result.version.local_path).is_file()
    assert db.query(RegDocumentVersion).count() == 1

    restore(db, document_id)

    assert fts_rows(db, document_id) == 1
    assert store.chunks.get(document_id)


def test_a_forced_reindex_does_not_resurrect_a_withdrawn_document(store, extracted):
    """`backfill_laws_fts` walks every document and writes its own rows.

    It does not go through `index_law_fts`, so a guard living there would have let
    `sbpeye laws reindex --force` put back exactly the rows `withdraw` had removed.
    """
    from sbpeye.search import backfill_laws_fts

    db = make_session()
    result = upload(db)
    document_id = result.document.id
    finish_upload(db, document_id)
    withdraw(db, document_id)
    assert fts_rows(db, document_id) == 0

    backfill_laws_fts(db, force=True)

    assert fts_rows(db, document_id) == 0

    restore(db, document_id)
    assert backfill_laws_fts(db, force=True) == 1
    assert fts_rows(db, document_id) == 1


def test_the_backlink_pass_is_scoped_to_the_document_just_uploaded(store, extracted):
    """`backlink_circulars(rescan=True)` would rescan 3,600 circulars to add one row."""
    db = make_session()
    other = RegDocument(
        id="other-law", title="Deposit Protection Corporation Act 2016",
        normalized_title="deposit protection corporation act 2016", doc_type="law",
    )
    db.add(other)
    db.add(Circular(
        id="c-1", reference="BPRD Circular No. 1 of 2024", title="Amendments",
        content_text=(
            "In exercise of the powers conferred by the Banking Companies Ordinance, "
            "1962, and having regard to the Deposit Protection Corporation Act 2016, "
            "the following amendments are made."
        ),
    ))
    db.add(Circular(id="c-2", reference="BPRD Circular No. 2 of 2024", title="Unrelated",
                    content_text="Guidance on branchless banking agents."))
    db.commit()

    result = upload(db)
    counts = finish_upload(db, result.document.id)

    assert counts["linked"] == 1
    links = {(link.circular_id, link.document_id) for link in db.query(
        laws_upload.RegDocument
    ).filter(laws_upload.RegDocument.id == result.document.id).one().circular_links}
    assert links == {("c-1", result.document.id)}
    # The other document is named in the same circular and must stay unlinked: this pass
    # is about one document, not a corpus rescan.
    assert other.circular_links == []


# ------------------------------------------------------------------ listing


def test_the_library_lists_uploads_and_the_rows_worth_uploading_to(store, extracted):
    db = make_session()
    db.add(RegDocument(id="ext-empty", title="Companies Ordinance 1984", is_external=1))
    db.add(RegDocument(id="sbp-held", title="PRs for SME Financing"))
    db.add(RegDocumentVersion(
        id="sbp-held-v1", document_id="sbp-held", content_hash="h", file_type="pdf",
        source="live", is_current=1, content_text="text",
    ))
    db.commit()
    uploaded = upload(db)

    listed = {document.id for document in upload_targets(db)}

    assert listed == {"ext-empty", uploaded.document.id}


# ------------------------------------------------------------------ the CLI


@pytest.fixture
def cli_db(monkeypatch, store):
    """The CLI's session factory pointed at one in-memory database."""
    from sqlalchemy.orm import sessionmaker

    import sbpeye.cli.commands as cli_module

    db = make_session()
    monkeypatch.setattr(
        cli_module, "SessionLocal", sessionmaker(bind=db.get_bind(), autoflush=False)
    )
    return db


def run_cli(*args):
    from click.testing import CliRunner

    from sbpeye.cli.commands import cli

    return CliRunner().invoke(cli, list(args))


def test_the_cli_uploads_a_file_and_says_what_happened(cli_db, extracted, tmp_path):
    source = tmp_path / "bco-1962.pdf"
    source.write_bytes(PDF_BYTES)

    result = run_cli(
        "laws", "upload", str(source),
        "--title", "Banking Companies Ordinance, 1962",
        "--type", "law",
        "--note", "Consolidated text from pakistancode.gov.pk, as of March 2024",
    )

    assert result.exit_code == 0, result.output
    assert "Created" in result.output
    assert "Extraction:   extracted" in result.output
    assert "In force:     yes" in result.output

    document = cli_db.query(RegDocument).one()
    assert document.origin == "upload"
    assert document.source_note.startswith("Consolidated text")
    assert document.versions[0].source == "upload"


def test_the_cli_records_the_run_so_it_appears_in_the_runs_tab(cli_db, extracted, tmp_path):
    """And under a kind the circular sync banner does not claim."""
    from sbpeye.models import SyncStatus, circular_sync_only

    source = tmp_path / "act.txt"
    source.write_text("Section 1. Short title.\n", encoding="utf-8")

    assert run_cli("laws", "upload", str(source), "--title", "Some Act, 2020").exit_code == 0

    job = cli_db.query(SyncStatus).one()
    assert job.kind == "laws_upload"
    assert job.status == "success"
    assert job.processed_count == 1
    assert cli_db.query(SyncStatus).filter(circular_sync_only()).count() == 0


def test_the_cli_reports_a_rejected_upload_without_recording_a_success(cli_db, tmp_path):
    from sbpeye.models import SyncStatus

    source = tmp_path / "act.docx"
    source.write_bytes(b"PK\x03\x04stuff")

    result = run_cli("laws", "upload", str(source), "--title", "Some Act, 2020")

    assert result.exit_code != 0
    assert "cannot be uploaded" in result.output
    assert cli_db.query(SyncStatus).one().status == "failed"
    assert cli_db.query(RegDocument).count() == 0


def test_the_cli_withdraws_and_restores_by_document_id(cli_db, extracted, tmp_path):
    source = tmp_path / "act.txt"
    source.write_text("Section 1. Short title.\n", encoding="utf-8")
    assert run_cli("laws", "upload", str(source), "--title", "Some Act, 2020").exit_code == 0
    document_id = cli_db.query(RegDocument).one().id

    assert run_cli("laws", "withdraw", document_id).exit_code == 0
    cli_db.expire_all()
    assert cli_db.query(RegDocument).one().delisted_at is not None

    assert run_cli("laws", "restore", document_id).exit_code == 0
    cli_db.expire_all()
    assert cli_db.query(RegDocument).one().delisted_at is None


def test_the_cli_refuses_an_unparseable_effective_date_before_touching_anything(
    cli_db, tmp_path
):
    source = tmp_path / "act.txt"
    source.write_text("Section 1.\n", encoding="utf-8")

    result = run_cli(
        "laws", "upload", str(source), "--title", "Some Act, 2020",
        "--effective-from", "next March",
    )

    assert result.exit_code != 0
    assert "not an ISO date" in result.output
    assert cli_db.query(RegDocument).count() == 0
