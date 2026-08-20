"""Phase 5 of the laws & regulations plan: search indexing and the laws API.

The vector arm is stubbed (embedding a corpus in a unit test would be slow and
non-deterministic); the FTS5 arm runs for real against SQLite, since that is where the
laws-vs-circulars isolation actually has to hold.

See docs/LAWS_REGULATIONS_PLAN.md.
"""

from datetime import datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import sbpeye.auth_routes as auth_routes_module
import sbpeye.main as main_module
from sbpeye.database import AppBase, Base, get_db
from sbpeye.models import Circular, RegDocument, RegDocumentLink, RegDocumentVersion
from sbpeye.search import (
    SearchEngine,
    backfill_laws_fts,
    delete_law_fts,
    index_law_fts,
    search_engine,
)

PR_TEXT = (
    "Prudential Regulations for SME Financing. Banks and DFIs shall ensure that the "
    "aggregate exposure of a bank against SME financing does not exceed the prescribed "
    "limit. Collateral requirements for small enterprise financing are relaxed."
)
FE_TEXT = (
    "Chapter 12 Exports. Authorized dealers shall ensure that export proceeds are "
    "repatriated to Pakistan within the prescribed period through the exchange record."
)


def make_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    # Runtime-state tables too: authentication puts `users` in the request path of every
    # route, so a corpus-only database now fails at the middleware rather than the query.
    AppBase.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS laws_fts USING fts5("
            "document_id UNINDEXED, title, part_label, body, tokenize='unicode61')"
        ))
        conn.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS circulars_fts USING fts5("
            "circular_id UNINDEXED, title, reference, body, tokenize='unicode61')"
        ))
    return sessionmaker(bind=engine, autoflush=False)(), engine


def add_law(db, document_id="doc-1", title="Prudential Regulations for SME Financing",
            text_body=PR_TEXT, doc_type="regulation", file_type="pdf",
            is_current=1, index=True, **doc_kwargs):
    document = RegDocument(
        id=document_id,
        title=title,
        normalized_title=title.casefold(),
        doc_type=doc_type,
        first_seen_at=datetime(2026, 8, 1),
        last_seen_at=datetime(2026, 8, 1),
        **doc_kwargs,
    )
    db.add(document)
    version = RegDocumentVersion(
        id=f"{document_id}-v1",
        document_id=document_id,
        content_hash=f"hash-{document_id}",
        file_type=file_type,
        content_text=text_body,
        is_current=is_current,
        first_seen_at=datetime(2026, 8, 1),
        last_seen_at=datetime(2026, 8, 1),
    )
    db.add(version)
    db.commit()
    if index:
        index_law_fts(db, document)
    return document, version


class EmptyCollection:
    """Stands in for the shared Chroma collection, matching nothing."""

    def query(self, **kwargs):
        return {"ids": [[]], "metadatas": [[]]}


@pytest.fixture
def no_vectors(monkeypatch):
    """Silence both vector arms so tests exercise the lexical path deterministically.

    Without this the arms hit the developer's real Chroma collection, whose ids resolve
    to nothing in an in-memory database.
    """
    monkeypatch.setattr("sbpeye.search.collection", EmptyCollection())
    monkeypatch.setattr(
        "sbpeye.search.embedding_backend.embed_queries", lambda queries: [[0.0]]
    )


# ------------------------------------------------------------------------- fts index


def test_indexing_makes_a_law_findable(no_vectors):
    db, _ = make_session()
    add_law(db)

    results, total = search_engine.search("SME financing collateral", db, source="laws")

    assert total == 1
    assert results[0]["result_kind"] == "law"
    assert results[0]["law"].title == "Prudential Regulations for SME Financing"
    assert "collateral" in results[0]["snippet"].lower()


def test_reindexing_replaces_rather_than_duplicates(no_vectors):
    db, engine = make_session()
    document, _ = add_law(db)

    index_law_fts(db, document)
    index_law_fts(db, document)

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT count(*) FROM laws_fts")).scalar()
    assert rows == 1


def test_only_the_version_in_force_is_searchable(no_vectors):
    """A hit on text SBP no longer publishes would be worse than no hit."""
    db, _ = make_session()
    document, old = add_law(db, text_body="The superseded rule mentions debentures.")

    new = RegDocumentVersion(
        id="doc-1-v2",
        document_id="doc-1",
        content_hash="hash-2",
        file_type="pdf",
        content_text="The revised rule mentions sukuk instead.",
        is_current=1,
        first_seen_at=datetime(2026, 8, 10),
    )
    old.is_current = 0
    db.add(new)
    db.commit()
    index_law_fts(db, document)

    _, superseded_hits = search_engine.search("debentures", db, source="laws")
    _, current_hits = search_engine.search("sukuk", db, source="laws")
    assert superseded_hits == 0
    assert current_hits == 1


def test_manifests_are_not_indexed(no_vectors):
    """A container's version is JSON bookkeeping, not readable law."""
    db, _ = make_session()
    add_law(
        db, "fe-manual", title="Foreign Exchange Manual",
        text_body='{"parts": [{"part_label": "Chapter 12"}]}', file_type="manifest",
    )

    _, total = search_engine.search("parts part_label", db, source="laws")
    assert total == 0


def test_a_document_with_nothing_in_force_indexes_nothing(no_vectors):
    db, engine = make_session()
    document = RegDocument(id="stub", title="Guidelines awaiting content",
                           doc_type="guideline", first_seen_at=datetime(2026, 8, 1))
    db.add(document)
    db.commit()
    index_law_fts(db, document)

    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM laws_fts")).scalar() == 1
    # Title-only: the row exists so the document is findable by name, with no body.
    results, total = search_engine.search("guidelines awaiting", db, source="laws")
    assert total == 1
    assert results[0]["version"] is None


def test_delete_removes_the_row(no_vectors):
    db, engine = make_session()
    document, _ = add_law(db)

    delete_law_fts(db, document.id)

    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM laws_fts")).scalar() == 0


def test_backfill_is_a_no_op_once_populated(no_vectors):
    db, _ = make_session()
    add_law(db, index=False)
    add_law(db, "doc-2", title="Prudential Regulations for Housing Finance", index=False)

    assert backfill_laws_fts(db) == 2
    assert backfill_laws_fts(db) == 0
    assert backfill_laws_fts(db, force=True) == 2


# ------------------------------------------------------------------- corpus isolation


def add_circular(db, circular_id="circ-1"):
    circular = Circular(
        id=circular_id,
        reference="SMEFD Circular No. 09 of 2026",
        title="Amendments in Prudential Regulations for SME Financing",
        department="SMEFD",
        date=datetime(2026, 3, 1),
        url=f"https://www.sbp.org.pk/circulars/{circular_id}",
        content_text="Banks are advised that the SME financing collateral rules change.",
    )
    db.add(circular)
    db.commit()
    from sbpeye.search import index_circular_fts

    index_circular_fts(db, circular)
    return circular


def test_the_default_source_returns_circulars_only(no_vectors):
    """The SPA cannot render law results yet, so nothing changes for it."""
    db, _ = make_session()
    add_circular(db)
    add_law(db)

    results, total = search_engine.search("SME financing", db)

    assert total == 1
    assert all(item["result_kind"] == "circular" for item in results)


def test_the_laws_source_returns_laws_only(no_vectors):
    db, _ = make_session()
    add_circular(db)
    add_law(db)

    results, total = search_engine.search("SME financing", db, source="laws")

    assert total == 1
    assert all(item["result_kind"] == "law" for item in results)


def test_the_all_source_merges_both_corpora(no_vectors):
    db, _ = make_session()
    add_circular(db)
    add_law(db)

    results, total = search_engine.search("SME financing", db, source="all")

    assert total == 2
    assert {item["result_kind"] for item in results} == {"circular", "law"}
    for item in results:
        assert ("circular" in item) ^ ("law" in item)


def test_an_unknown_source_is_rejected():
    db, _ = make_session()
    with pytest.raises(ValueError):
        search_engine.search("anything", db, source="nonsense")


def test_doc_type_filters_the_law_arm(no_vectors):
    db, _ = make_session()
    add_law(db, "doc-1", doc_type="regulation")
    add_law(db, "doc-2", title="Guidelines for SME financing risk", doc_type="guideline")

    _, regulations = search_engine.search("SME financing", db, source="laws", doc_type="regulation")
    _, guidelines = search_engine.search("SME financing", db, source="laws", doc_type="guideline")
    _, everything = search_engine.search("SME financing", db, source="laws")

    assert (regulations, guidelines, everything) == (1, 1, 2)


def test_an_empty_query_browses_the_requested_corpus(no_vectors):
    db, _ = make_session()
    add_circular(db)
    add_law(db)

    law_results, law_total = search_engine.search("", db, source="laws")
    circular_results, _ = search_engine.search("", db)

    assert law_total == 1
    assert law_results[0]["result_kind"] == "law"
    assert circular_results[0]["result_kind"] == "circular"


def test_delisted_documents_are_left_out_of_empty_query_browse(no_vectors):
    db, _ = make_session()
    add_law(db, "doc-1")
    add_law(db, "doc-2", title="Withdrawn guidance", delisted_at=datetime(2026, 8, 5))

    _, total = search_engine.search("", db, source="laws")
    assert total == 1


def test_the_circular_vector_arm_excludes_law_chunks(monkeypatch):
    """Law chunks share the Chroma collection and must never enter circular candidates."""
    db, _ = make_session()
    captured = {}

    class FakeCollection:
        def query(self, **kwargs):
            captured.update(kwargs)
            return {"ids": [[]], "metadatas": [[]]}

    monkeypatch.setattr("sbpeye.search.collection", FakeCollection())
    monkeypatch.setattr(
        "sbpeye.search.embedding_backend.embed_queries", lambda queries: [[0.0]]
    )
    monkeypatch.setattr(SearchEngine, "_law_vector_ranks", lambda self, query: ({}, {}))

    search_engine.search("SME financing", db)

    assert captured["where"] == {"doc_type": {"$in": ["circular", "attachment"]}}


def test_the_law_vector_arm_filters_to_law_chunks(monkeypatch):
    db, _ = make_session()
    captured = {}

    class FakeCollection:
        def query(self, **kwargs):
            captured.update(kwargs)
            return {"ids": [[]], "metadatas": [[]]}

    monkeypatch.setattr("sbpeye.search.collection", FakeCollection())
    monkeypatch.setattr(
        "sbpeye.search.embedding_backend.embed_queries", lambda queries: [[0.0]]
    )

    search_engine.search("SME financing", db, source="laws")

    assert captured["where"] == {"kind": "law"}


# ------------------------------------------------------------------------------- api


@pytest.fixture
def client(monkeypatch):
    """TestClient wired to an isolated DB with both FTS tables present."""
    db, engine = make_session()
    factory = sessionmaker(bind=engine, autoflush=False)

    def override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(main_module, "SessionLocal", factory)
    # The startup warm-up backfills both FTS tables in a background thread; against a
    # test DB it races with the rows each test indexes for itself.
    monkeypatch.setattr(main_module, "_warm_up_search_index", lambda: None)
    monkeypatch.setattr("sbpeye.search.collection", EmptyCollection())
    monkeypatch.setattr(
        "sbpeye.search.embedding_backend.embed_queries", lambda queries: [[0.0]]
    )
    main_module.app.dependency_overrides[get_db] = override_get_db
    # `resolve_request_user` opens its own session, so it has to be pointed at this DB
    # too or the middleware looks the signed-in user up in the developer's real one.
    monkeypatch.setattr(auth_routes_module, "AppSessionLocal", factory)
    from fastapi.testclient import TestClient

    with TestClient(main_module.app) as test_client:
        # This fixture builds its own client rather than using conftest's, so it has to
        # establish a session itself; every route sits behind the auth middleware.
        from conftest import sign_in

        sign_in(test_client, factory, is_admin=True)
        yield test_client, db
    main_module.app.dependency_overrides.clear()


def test_api_lists_laws_with_filters(client):
    test_client, db = client
    add_law(db, "doc-1", doc_type="regulation")
    add_law(db, "doc-2", title="Guidelines for Clearing Operations", doc_type="guideline")

    payload = test_client.get("/api/laws").json()
    assert payload["total"] == 2
    assert {item["doc_type"] for item in payload["items"]} == {"regulation", "guideline"}

    filtered = test_client.get("/api/laws", params={"doc_type": "guideline"}).json()
    assert filtered["total"] == 1
    assert filtered["items"][0]["title"] == "Guidelines for Clearing Operations"


def test_api_list_can_hide_the_parts_of_containers(client):
    test_client, db = client
    add_law(db, "fe-manual", title="Foreign Exchange Manual", file_type="manifest")
    add_law(db, "chapter-12", title="EXPORTS", text_body=FE_TEXT,
            parent_id="fe-manual", part_label="Chapter 12", part_order=12)

    flat = test_client.get("/api/laws", params={"top_level": True}).json()
    assert [item["title"] for item in flat["items"]] == ["Foreign Exchange Manual"]

    parts = test_client.get("/api/laws", params={"parent_id": "fe-manual"}).json()
    assert [item["part_label"] for item in parts["items"]] == ["Chapter 12"]


def test_api_search_uses_the_law_corpus(client):
    test_client, db = client
    add_law(db)

    payload = test_client.get("/api/laws", params={"q": "collateral"}).json()

    assert payload["total"] == 1
    assert payload["items"][0]["result_kind"] == "law"
    assert payload["items"][0]["snippet"]


def test_api_detail_carries_timeline_children_and_links(client):
    test_client, db = client
    document, version = add_law(db, "fe-manual", title="Foreign Exchange Manual",
                                file_type="manifest")
    add_law(db, "chapter-12", title="EXPORTS", text_body=FE_TEXT,
            parent_id="fe-manual", part_label="Chapter 12", part_order=12)
    circular = add_circular(db)
    db.add(RegDocumentLink(circular_id=circular.id, document_id="fe-manual",
                           link_type="amends", detected_via="ai", confidence=0.8))
    db.add(RegDocumentVersion(
        id="fe-manual-v0", document_id="fe-manual", content_hash="older",
        file_type="manifest", is_current=0, first_seen_at=datetime(2026, 7, 1),
    ))
    db.commit()

    payload = test_client.get("/api/laws/fe-manual").json()

    assert payload["id"] == "fe-manual"
    assert payload["current_version"]["content_hash"] == "hash-fe-manual"
    assert [v["content_hash"] for v in payload["versions"]] == ["hash-fe-manual", "older"]
    assert [c["part_label"] for c in payload["children"]] == ["Chapter 12"]
    assert payload["linked_circulars"][0]["link_type"] == "amends"
    assert payload["linked_circulars"][0]["circular"]["reference"] == "SMEFD Circular No. 09 of 2026"


def test_api_detail_of_a_circular_backed_row_points_at_the_circular(client):
    test_client, db = client
    circular = add_circular(db)
    document = RegDocument(id="doc-guideline", title="Branch Licensing Policy",
                           doc_type="guideline", circular_id=circular.id,
                           first_seen_at=datetime(2026, 8, 1))
    db.add(document)
    db.commit()

    payload = test_client.get("/api/laws/doc-guideline").json()

    assert payload["circular"]["id"] == circular.id
    assert payload["current_version"] is None
    assert payload["versions"] == []


def test_api_version_detail_exposes_text_and_pending_state(client):
    test_client, db = client
    add_law(db)
    db.add(RegDocumentVersion(
        id="doc-1-future", document_id="doc-1", content_hash="future",
        file_type="pdf", content_text="Applies next year.",
        effective_from=datetime(2027, 1, 1), is_current=0,
        first_seen_at=datetime(2026, 8, 1),
    ))
    db.commit()

    current = test_client.get("/api/laws/doc-1/versions/doc-1-v1").json()
    assert current["content_text"].startswith("Prudential Regulations")
    assert current["is_current"] is True
    assert current["pending"] is False
    assert current["document"]["title"] == "Prudential Regulations for SME Financing"

    pending = test_client.get("/api/laws/doc-1/versions/doc-1-future").json()
    assert pending["pending"] is True
    assert pending["is_current"] is False


def test_api_returns_404_for_unknown_ids(client):
    test_client, db = client
    add_law(db)

    assert test_client.get("/api/laws/nope").status_code == 404
    assert test_client.get("/api/laws/doc-1/versions/nope").status_code == 404
    # A version id that exists but under a different document must not leak across.
    add_law(db, "doc-2", title="Another regulation")
    assert test_client.get("/api/laws/doc-1/versions/doc-2-v1").status_code == 404


def test_api_file_route_refuses_paths_outside_the_archive(client, tmp_path):
    test_client, db = client
    _, version = add_law(db)
    version.local_path = "../../etc/passwd"
    db.commit()

    assert test_client.get("/api/laws/doc-1/file").status_code == 404


def test_api_types_facet(client):
    test_client, db = client
    add_law(db, "doc-1", doc_type="regulation")
    add_law(db, "doc-2", title="A guideline", doc_type="guideline")
    add_law(db, "doc-3", title="Another guideline", doc_type="guideline")

    facets = test_client.get("/api/laws/types").json()

    assert facets[0] == {"doc_type": "guideline", "count": 2}
    assert {"doc_type": "regulation", "count": 1} in facets


def test_circular_search_endpoint_stays_circular_by_default(client):
    test_client, db = client
    add_circular(db)
    add_law(db)

    default = test_client.get("/api/circulars/search", params={"q": "SME financing"}).json()
    assert {item["result_kind"] for item in default["items"]} == {"circular"}

    mixed = test_client.get(
        "/api/circulars/search", params={"q": "SME financing", "source": "all"}
    ).json()
    assert {item["result_kind"] for item in mixed["items"]} == {"circular", "law"}

    rejected = test_client.get(
        "/api/circulars/search", params={"q": "x", "source": "bogus"}
    )
    assert rejected.status_code == 400


# ------------------------------------------------- the two gaps closed in step 2


def test_api_can_order_laws_by_when_we_captured_them(client):
    """`sort_by=captured` — newest capture first, and what we hold nothing for last.

    This is the ordering behind "what moved recently", the one question SBP's own site
    cannot answer: it replaces files in place and keeps no history.
    """
    test_client, db = client
    add_law(db, "old", title="Credit Bureau Act 2015", doc_type="law")
    add_law(db, "new", title="Guidelines for Clearing Operations", doc_type="guideline")
    for document_id, captured in (("old", datetime(2026, 7, 1)), ("new", datetime(2026, 8, 10))):
        db.query(RegDocumentVersion).filter(
            RegDocumentVersion.document_id == document_id
        ).update({"first_seen_at": captured})
    # An externally-hosted stub with no version at all: still listed, but never first.
    db.add(RegDocument(id="stub", title="Banking Companies Ordinance 1962",
                       doc_type="law", is_external=1, first_seen_at=datetime(2026, 8, 1)))
    db.commit()

    payload = test_client.get("/api/laws", params={"sort_by": "captured"}).json()

    assert payload["total"] == 3
    assert [item["id"] for item in payload["items"]] == ["new", "old", "stub"]

    # An unknown or absent sort_by leaves the existing ordering untouched.
    for params in ({}, {"sort_by": "title"}, {"sort_by": "nonsense"}):
        default = test_client.get("/api/laws", params=params).json()
        assert [item["id"] for item in default["items"]] == ["new", "stub", "old"]


def test_api_circular_detail_lists_the_regulations_it_cites(client):
    """The reverse of a law's `linked_circulars`: 809 edges, read circular-first."""
    test_client, db = client
    add_law(db, "fe-manual", title="Foreign Exchange Manual", file_type="manifest")
    add_law(db, "chapter-12", title="EXPORTS", text_body=FE_TEXT,
            parent_id="fe-manual", part_label="Chapter 12", part_order=12)
    circular = add_circular(db)
    db.add(RegDocumentLink(circular_id=circular.id, document_id="chapter-12",
                           link_type="references", detected_via="url_scan", confidence=0.9))
    # The same pair found twice; the payload must name the document once, via the
    # more confident edge.
    db.add(RegDocumentLink(circular_id=circular.id, document_id="chapter-12",
                           link_type="listing", detected_via="listing", confidence=0.5))
    db.commit()

    payload = test_client.get(f"/api/circulars/{circular.id}").json()

    assert len(payload["regulations"]) == 1
    cited = payload["regulations"][0]
    assert cited["link_type"] == "references"
    assert cited["detected_via"] == "url_scan"
    assert cited["document"]["id"] == "chapter-12"
    assert cited["document"]["display_title"] == "EXPORTS"
    # A part is never shown without its container.
    assert cited["document"]["parent_title"] == "Foreign Exchange Manual"


def test_api_circular_detail_regulations_is_empty_not_absent(client):
    """A circular citing nothing still carries the key, so the UI need not guard for it."""
    test_client, db = client
    circular = add_circular(db)

    payload = test_client.get(f"/api/circulars/{circular.id}").json()

    assert payload["regulations"] == []


def test_api_circular_regulations_dedupe_does_not_depend_on_row_order(client):
    """Every duplicate pair in the corpus has null confidence on both edges, so the
    tiebreak has to come from somewhere other than which row was inserted first."""
    test_client, db = client
    add_law(db, "fe-manual", title="Foreign Exchange Manual", file_type="manifest")
    circular = add_circular(db)
    # Inserted weakest-first, so row order would pick the wrong one.
    db.add(RegDocumentLink(circular_id=circular.id, document_id="fe-manual",
                           link_type="references", detected_via="name_match"))
    db.add(RegDocumentLink(circular_id=circular.id, document_id="fe-manual",
                           link_type="references", detected_via="url_scan"))
    db.commit()

    payload = test_client.get(f"/api/circulars/{circular.id}").json()

    assert len(payload["regulations"]) == 1
    assert payload["regulations"][0]["detected_via"] == "url_scan"


def test_api_circular_regulations_group_parts_under_their_container(client):
    """A circular revising four chapters should read as the manual, then its chapters in
    chapter order — not as an alphabetical list with the manual stranded in the middle."""
    test_client, db = client
    add_law(db, "fe-manual", title="Foreign Exchange Manual", file_type="manifest")
    for document_id, label, order, title in (
        ("ch-9", "Chapter 9", 9, "BLOCKED ACCOUNTS"),
        ("ch-1", "Chapter 1", 1, "INTRODUCTORY"),
        ("ch-11", "Chapter 11", 11, "DEALINGS IN FOREIGN CURRENCY NOTES"),
    ):
        add_law(db, document_id, title=title, text_body=FE_TEXT,
                parent_id="fe-manual", part_label=label, part_order=order)
    circular = add_circular(db)
    for document_id in ("ch-9", "ch-1", "fe-manual", "ch-11"):
        db.add(RegDocumentLink(circular_id=circular.id, document_id=document_id,
                               link_type="references", detected_via="name_match"))
    db.commit()

    payload = test_client.get(f"/api/circulars/{circular.id}").json()

    assert [r["document"]["id"] for r in payload["regulations"]] == [
        "fe-manual", "ch-1", "ch-9", "ch-11",
    ]
