"""Phase 3 of the laws uploads plan: the console can do everything the CLI can.

The background half of an upload (indexing plus the scoped backlink scan) is stubbed so
the route tests assert what the route does, not what the thread does — that is
test_laws_upload.py's subject.

See docs/LAWS_UPLOADS_PLAN.md.
"""

import hashlib

import pytest

from sbpeye.models import RegDocument, RegDocumentVersion, SyncStatus

PDF_BYTES = b"%PDF-1.7\nAn Act to consolidate the law relating to banking companies.\n"


@pytest.fixture
def upload_client(monkeypatch, tmp_path):
    """An admin TestClient with the archive in tmp_path and the finishing thread stubbed."""
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    import sbpeye.auth_routes as auth_routes_module
    import sbpeye.laws_upload as laws_upload_module
    import sbpeye.main as main_module
    from sbpeye.database import get_db
    from sbpeye.scraper import laws
    from test_laws_search import EmptyCollection, make_session

    db, engine = make_session()
    factory = sessionmaker(bind=engine, autoflush=False)

    def override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(laws_upload_module, "LAWS_ARCHIVE_DIR", tmp_path / "files" / "laws")
    monkeypatch.setattr(laws_upload_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(laws, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        laws_upload_module, "extract_document_text",
        lambda path, file_type: (path.read_bytes().decode("latin-1"), "extracted", None),
    )

    finished: list[str] = []
    monkeypatch.setattr(main_module, "SessionLocal", factory)
    monkeypatch.setattr(main_module, "_warm_up_search_index", lambda: None)
    monkeypatch.setattr(
        main_module, "_finish_law_upload",
        lambda job_id, document_id: finished.append(document_id),
    )
    monkeypatch.setattr("sbpeye.search.collection", EmptyCollection())
    main_module.app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(auth_routes_module, "AppSessionLocal", factory)

    with TestClient(main_module.app) as test_client:
        from conftest import sign_in

        admin = sign_in(test_client, factory, is_admin=True)
        yield test_client, db, finished, admin
    main_module.app.dependency_overrides.clear()


def post_upload(test_client, *, body=PDF_BYTES, filename="bco-1962.pdf", **fields):
    data = {"title": "Banking Companies Ordinance, 1962", "doc_type": "law"}
    data.update({key: value for key, value in fields.items() if value is not None})
    return test_client.post(
        "/api/laws/upload",
        files={"file": (filename, body, "application/pdf")},
        data=data,
    )


# ------------------------------------------------------------------ permissions


def test_a_non_admin_cannot_upload_resolve_withdraw_or_pin(upload_client):
    from conftest import sign_in

    import sbpeye.auth_routes as auth_routes_module

    test_client, db, _finished, _admin = upload_client
    # Replace the fixture's admin session with a plain tester's: every route below is
    # admin-only, and a 403 is the whole assertion.
    sign_in(test_client, auth_routes_module.AppSessionLocal, is_admin=False)

    assert post_upload(test_client).status_code == 403
    assert test_client.post("/api/laws/upload/resolve", json={"title": "X"}).status_code == 403
    assert test_client.get("/api/laws/uploads").status_code == 403
    assert test_client.post("/api/laws/doc-1/withdraw").status_code == 403
    assert test_client.post("/api/laws/doc-1/restore").status_code == 403
    assert test_client.post("/api/laws/doc-1/versions/v-1/pin").status_code == 403
    assert test_client.post("/api/laws/doc-1/versions/v-1/unpin").status_code == 403
    assert db.query(RegDocument).count() == 0


# ------------------------------------------------------------------ resolve


def test_resolve_previews_the_attachment_before_anything_is_written(upload_client):
    from sbpeye.scraper.laws import law_identity, normalize_law_title

    test_client, db, _finished, _admin = upload_client
    title = "Banking Companies Ordinance, 1962"
    db.add(RegDocument(
        id=law_identity(title), title=title, normalized_title=normalize_law_title(title),
        doc_type="law", is_external=1,
    ))
    db.commit()

    response = test_client.post("/api/laws/upload/resolve", json={"title": title})

    assert response.status_code == 200
    payload = response.json()
    assert payload["exists"]
    assert payload["is_external"]
    assert not payload["holds_text"]
    assert "no text held" in payload["summary"]
    # A preview writes nothing.
    assert db.query(RegDocumentVersion).count() == 0


def test_resolve_reports_a_title_it_has_never_seen_as_a_new_document(upload_client):
    test_client, _db, _finished, _admin = upload_client

    payload = test_client.post(
        "/api/laws/upload/resolve", json={"title": "Companies Act, 2017"}
    ).json()

    assert not payload["exists"]
    assert payload["summary"] == "New document."


def test_resolve_refuses_an_id_for_no_document(upload_client):
    test_client, _db, _finished, _admin = upload_client

    response = test_client.post(
        "/api/laws/upload/resolve", json={"title": "X Act", "document_id": "nope"}
    )

    assert response.status_code == 400
    assert "No document with id" in response.json()["error"]


# ------------------------------------------------------------------ upload


def test_the_upload_response_says_whether_the_text_will_be_searchable(upload_client):
    test_client, db, finished, admin = upload_client

    response = post_upload(
        test_client, source_note="Consolidated text from pakistancode.gov.pk, March 2024"
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["created_document"]
    assert payload["will_be_searchable"]
    assert payload["extraction_status"] == "extracted"
    assert payload["is_current"]
    assert payload["indexing"] == "queued"
    assert payload["version"]["source"] == "upload"
    assert payload["version"]["uploaded_by"] == admin.id
    assert payload["version"]["original_filename"] == "bco-1962.pdf"
    assert payload["document"]["origin"] == "upload"
    assert payload["document"]["source_note"].startswith("Consolidated text")
    # Indexing is deferred to the thread, under a run row the Runs tab can show.
    assert finished == [payload["document"]["id"]]
    job = db.query(SyncStatus).one()
    assert job.kind == "laws_upload"
    assert payload["job_id"] == job.job_id


def test_a_scanned_upload_says_so_in_the_same_response(upload_client, monkeypatch):
    import sbpeye.laws_upload as laws_upload_module

    test_client, _db, _finished, _admin = upload_client
    monkeypatch.setattr(
        laws_upload_module, "extract_document_text",
        lambda path, file_type: (None, "scanned", None),
    )

    payload = post_upload(test_client).json()

    assert payload["extraction_status"] == "scanned"
    assert not payload["will_be_searchable"]


def test_an_oversized_upload_is_refused_without_being_stored(upload_client, monkeypatch):
    import sbpeye.main as main_module

    test_client, db, _finished, _admin = upload_client
    monkeypatch.setenv("LAWS_UPLOAD_MAX_BYTES", str(len(PDF_BYTES) - 1))

    response = post_upload(test_client)

    assert response.status_code == 413
    # In bytes, not "the 0 MB limit": the cap here is smaller than a megabyte.
    assert response.json()["error"] == (
        f"The file is larger than the {len(PDF_BYTES) - 1} bytes limit."
    )
    assert db.query(RegDocument).count() == 0
    assert main_module._laws_upload_max_bytes() == len(PDF_BYTES) - 1


def test_a_rejected_file_type_is_a_400_with_the_reason(upload_client):
    test_client, db, _finished, _admin = upload_client

    response = post_upload(test_client, body=b"PK\x03\x04zip", filename="act.docx")

    assert response.status_code == 400
    assert "cannot be uploaded" in response.json()["error"]
    assert db.query(RegDocument).count() == 0


def test_a_duplicate_returns_the_existing_version_and_queues_no_indexing(upload_client):
    test_client, db, finished, _admin = upload_client
    first = post_upload(test_client).json()
    finished.clear()

    again = post_upload(test_client, filename="bco-copy.pdf")

    assert again.status_code == 200
    payload = again.json()
    assert payload["duplicate"]
    assert payload["indexing"] == "skipped"
    assert payload["version"]["id"] == first["version"]["id"]
    assert finished == []
    assert db.query(RegDocumentVersion).count() == 1


def test_an_unparseable_effective_date_is_refused_before_the_file_is_stored(upload_client):
    test_client, db, _finished, _admin = upload_client

    response = post_upload(test_client, effective_from="next March")

    assert response.status_code == 400
    assert "not an ISO date" in response.json()["error"]
    assert db.query(RegDocument).count() == 0


# ------------------------------------------------------------------ state changes


def test_withdraw_and_restore_flip_the_document_both_ways(upload_client):
    test_client, db, _finished, _admin = upload_client
    document_id = post_upload(test_client).json()["document"]["id"]

    withdrawn = test_client.post(f"/api/laws/{document_id}/withdraw")
    assert withdrawn.status_code == 200
    assert withdrawn.json()["delisted_at"] is not None

    restored = test_client.post(f"/api/laws/{document_id}/restore")
    assert restored.status_code == 200
    assert restored.json()["delisted_at"] is None
    # Nothing was deleted on the way through.
    assert db.query(RegDocumentVersion).count() == 1


def test_pinning_through_the_api_swaps_which_edition_is_in_force(upload_client):
    from datetime import datetime

    from sbpeye.scraper.laws import law_identity, normalize_law_title

    test_client, db, _finished, _admin = upload_client
    title = "Banks Nationalization Act 1974"
    document_id = law_identity(title)
    db.add(RegDocument(
        id=document_id, title=title, normalized_title=normalize_law_title(title),
        doc_type="law",
    ))
    db.add(RegDocumentVersion(
        id="sbp-scanned", document_id=document_id, content_hash="sbp-hash",
        file_type="pdf", source="live", is_current=1, extraction_status="scanned",
        first_seen_at=datetime(2024, 1, 1),
    ))
    db.commit()

    uploaded = post_upload(test_client, title=title, filename="bna-1974.pdf").json()
    version_id = uploaded["version"]["id"]
    assert not uploaded["is_current"]

    pinned = test_client.post(f"/api/laws/{document_id}/versions/{version_id}/pin")
    assert pinned.status_code == 200
    assert pinned.json()["current_version"]["id"] == version_id
    assert pinned.json()["current_version"]["pinned"]

    unpinned = test_client.post(f"/api/laws/{document_id}/versions/{version_id}/unpin")
    assert unpinned.status_code == 200
    assert unpinned.json()["current_version"]["id"] == "sbp-scanned"


def test_a_state_change_on_an_unknown_id_is_a_404(upload_client):
    test_client, _db, _finished, _admin = upload_client

    assert test_client.post("/api/laws/nope/withdraw").status_code == 404
    assert test_client.post("/api/laws/nope/versions/nope/pin").status_code == 404


def test_pinning_refuses_a_version_that_belongs_to_another_document(upload_client):
    """A version id is unique on its own, so the document half of the URL has to be
    checked — otherwise a stale or wrong id pins someone else's edition and the response
    hands back that other document, a URL that lies about what it changed."""
    test_client, db, _finished, _admin = upload_client
    db.add(RegDocument(id="someone-else", title="Companies Ordinance 1984"))
    db.commit()
    version_id = post_upload(test_client).json()["version"]["id"]

    response = test_client.post(f"/api/laws/someone-else/versions/{version_id}/pin")

    assert response.status_code == 404
    assert "not someone-else" in response.json()["error"]
    assert db.query(RegDocumentVersion).filter(RegDocumentVersion.pinned == 1).count() == 0


# ------------------------------------------------------------------ listing


def test_the_uploads_list_carries_uploads_and_the_rows_worth_uploading_to(upload_client):
    test_client, db, _finished, _admin = upload_client
    db.add(RegDocument(id="ext-empty", title="Companies Ordinance 1984", is_external=1))
    db.commit()
    uploaded = post_upload(test_client).json()["document"]["id"]

    rows = test_client.get("/api/laws/uploads").json()

    assert {row["id"] for row in rows} == {"ext-empty", uploaded}
    mine = next(row for row in rows if row["id"] == uploaded)
    assert mine["versions"][0]["source"] == "upload"
    assert mine["versions"][0]["content_hash"] == hashlib.sha256(PDF_BYTES).hexdigest()
