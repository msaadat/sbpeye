"""Phase 2 of the laws & regulations plan: the listing scraper and content versioning.

Network access is stubbed throughout — the listing comes from a trimmed capture of the
real page (tests/fixtures/laws_listing.html) and file downloads come from a fake byte
store, so a run is deterministic and offline.

See docs/LAWS_REGULATIONS_PLAN.md.
"""

import hashlib
from datetime import datetime
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sbpeye.database import Base
from sbpeye.models import RegDocument, RegDocumentVersion
from sbpeye.scraper import laws

FIXTURE = Path(__file__).parent / "fixtures" / "laws_listing.html"
PDF_HEADER = b"%PDF-1.7\n"


def make_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False)()


def listing_rows():
    return laws.parse_listing(BeautifulSoup(FIXTURE.read_bytes(), "html.parser"))


@pytest.fixture
def fake_site(monkeypatch, tmp_path):
    """A fake SBP: the fixture listing plus an editable url -> bytes file store."""

    class Site:
        def __init__(self):
            self.files: dict[str, bytes] = {}
            self.downloads: list[str] = []
            self.listing_fetches = 0

        def serve(self, url: str, body: bytes):
            self.files[url] = body

    site = Site()

    def fake_fetch_page_cached(url, force=False):
        site.listing_fetches += 1
        return FIXTURE.read_bytes()

    def fake_download(document_id, url, force=False):
        site.downloads.append(url)
        body = site.files.get(url)
        if body is None:
            return None, None, f"404 for {url}"
        content_hash = hashlib.sha256(body).hexdigest()
        destination = tmp_path / document_id / laws._archive_name(content_hash, url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)
        return destination, content_hash, None

    monkeypatch.setattr(laws, "fetch_page_cached", fake_fetch_page_cached)
    monkeypatch.setattr(laws, "download_law_file", fake_download)
    monkeypatch.setattr(laws, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        laws, "extract_document_text",
        lambda path, file_type: (path.read_bytes().decode("latin-1"), "extracted", None),
    )
    return site


# --------------------------------------------------------------- title normalization


@pytest.mark.parametrize("title,expected", [
    ("Prudential Regulations for SME Financing (Updated till July 16, 2026)",
     "prudential regulations for sme financing"),
    ("Prudential Regulations for SME Financing, updated till October 07, 2024",
     "prudential regulations for sme financing"),
    ("Prudential Regulations for SME Financing (to be applicable from January 1, 2026)",
     "prudential regulations for sme financing"),
    ("Prudential Regulations for Microfinance Banks (Updated on May 2025)",
     "prudential regulations for microfinance banks"),
    ("Prudential Regulations for Consumer Financing (as of August 03, 2016)",
     "prudential regulations for consumer financing"),
    ("Banks Nationalization Act 1974 (as modified up to June 30, 2007)",
     "banks nationalization act 1974"),
    ("Banking Companies Ordinance 1962 (being updated)",
     "banking companies ordinance 1962"),
    ("Deposit Protection Corporation Act 2016 ( being updated )",
     "deposit protection corporation act 2016"),
    ("State Bank of Pakistan (Banking Services Corporation) Ordinance, 2001.",
     "state bank of pakistan (banking services corporation) ordinance, 2001"),
    ("  Prudential Regulations for Corporate/ Commercial Banking   FAQs ",
     "prudential regulations for corporate/commercial banking faqs"),
])
def test_normalize_law_title_strips_only_version_metadata(title, expected):
    assert laws.normalize_law_title(title) == expected


@pytest.mark.parametrize("title", [
    "State Bank of Pakistan Act, 1956",
    "Credit Bureau Act 2015",
    "Credit Bureaus Amendment Act 2016",
    "Guidelines for Coordinated Portfolio Investment Survey (CPIS)",
    "Reporting Guides - Monetary and Financial Statistics",
    "AML/CFT/CPF Regulations - Guidelines on Targeted Financial Sanctions (TFS) under UNSC Resolutions",
])
def test_titles_that_name_the_document_survive_normalization(title):
    """A year or a parenthetical that is part of the name is identity, not version metadata."""
    normalized = laws.normalize_law_title(title)
    assert normalized == laws._tidy(title).casefold().rstrip(" .,-")


def test_documents_distinguished_only_by_year_keep_separate_identities():
    assert laws.law_identity("Credit Bureau Act 2015") != laws.law_identity(
        "Credit Bureaus Amendment Act 2016"
    )


def test_parallel_editions_resolve_to_one_identity():
    """The SME PRs case: two live rows, one document."""
    october = "Prudential Regulations for SME Financing, updated till October 07, 2024"
    january = "Prudential Regulations for SME Financing (to be applicable from January 1, 2026)"
    assert laws.law_identity(october) == laws.law_identity(january)


@pytest.mark.parametrize("title,label", [
    ("PRs for MFBs (Updated on May 2025)", "Updated on May 2025"),
    ("PRs for Corporate Banking (Updated till June 2024)", "Updated till June 2024"),
    ("PRs for Consumer Financing (as of August 03, 2016)", "as of August 03, 2016"),
    ("PRs for SME Financing, updated till October 07, 2024", "updated till October 07, 2024"),
    ("Guidelines for Clearing Operations", None),
])
def test_parse_version_label(title, label):
    assert laws.parse_version_label(title) == label


@pytest.mark.parametrize("title,expected", [
    ("PRs for SME Financing (to be applicable from January 1, 2026)", datetime(2026, 1, 1)),
    ("PRs for SME Financing (applicable from 15 March 2027)", datetime(2027, 3, 15)),
    ("PRs for SME Financing (effective from July 2026)", datetime(2026, 7, 1)),
    # "Updated till <date>" labels a revision already in force, not a future one.
    ("PRs for SME Financing (Updated till July 16, 2026)", None),
    ("PRs for MFBs (Updated on May 2025)", None),
    ("Guidelines for Clearing Operations", None),
])
def test_parse_effective_from(title, expected):
    assert laws.parse_effective_from(title) == expected


# ------------------------------------------------------------------- listing parsing


def test_parse_listing_reads_metadata_from_row_attributes():
    rows = listing_rows()
    assert len(rows) == 10

    first = rows[0]
    assert first["title"] == "State Bank of Pakistan Act, 1956"
    assert first["doc_type"] == "law"
    assert first["listed_date"] == datetime(1956, 12, 30)
    assert first["url"].endswith("/laws_regulations/SBP-Act.pdf")
    assert first["route"] == laws.ROUTE_PDF
    assert first["order"] == 0


def test_parse_listing_maps_every_section_to_a_doc_type():
    assert {row["doc_type"] for row in listing_rows()} == {"law", "regulation", "guideline"}
    assert laws.DOC_TYPES["gazette notifications"] == "gazette"
    assert laws.DOC_TYPES["licensing guidelines"] == "licensing"


def test_parse_listing_collapses_the_duplicate_link_a_row_carries():
    """Subpage rows link the same slug twice (icon + "View details")."""
    manual = next(r for r in listing_rows() if r["title"] == "Foreign Exchange Manual")
    assert manual["route"] == laws.ROUTE_SUBPAGE
    assert manual["url"] == "https://www.sbp.org.pk/laws-regulations/foreign-exchange-manual"


def test_parse_listing_tolerates_a_missing_date():
    manual = next(r for r in listing_rows() if r["title"] == "Foreign Exchange Manual")
    assert manual["listed_date"] is None


@pytest.mark.parametrize("url,route", [
    ("https://www.sbp.org.pk/assets/documents/laws_regulations/SBP-Act.pdf", laws.ROUTE_PDF),
    ("https://www.sbp.org.pk/assets/document/Chapter-12-foreign-exchange-manual.pdf", laws.ROUTE_PDF),
    ("https://www.sbp.org.pk/laws-regulations/foreign-exchange-manual", laws.ROUTE_SUBPAGE),
    ("https://www.sbp.org.pk/circulars/ibd-circular-no-04-of-2020", laws.ROUTE_CIRCULAR),
    ("https://pakistancode.gov.pk/english/UY2FqaJw1", laws.ROUTE_EXTERNAL),
    ("http://www.sbp.org.pk/assets/documents/laws_regulations/SBP-Act.pdf", laws.ROUTE_EXTERNAL),
    ("https://www.sbp.org.pk/laws-regulations", laws.ROUTE_UNKNOWN),
    (None, laws.ROUTE_UNKNOWN),
])
def test_route_link_classifies_every_destination_type(url, route):
    assert laws.route_link(url) == route


# ---------------------------------------------------------------------------- sync


def serve_all_pdfs(site, rows, body=PDF_HEADER + b"v1"):
    for row in rows:
        if row["route"] == laws.ROUTE_PDF:
            site.serve(row["url"], body + row["url"].encode())


def test_sync_captures_documents_versions_and_stubs(fake_site):
    db = make_session()
    serve_all_pdfs(fake_site, listing_rows())

    counts = laws.sync_laws(db, delay=0, verbose=False)

    assert counts["documents"] == 10
    assert counts["new_versions"] == 6
    assert counts["stubs"] == 3      # the FE Manual subpage and two circular-typed rows
    assert counts["external"] == 1   # pakistancode.gov.pk
    assert counts["errors"] == 0

    assert db.query(RegDocument).count() == 10
    assert db.query(RegDocumentVersion).count() == 6

    external = db.query(RegDocument).filter(RegDocument.is_external == 1).one()
    assert external.title.startswith("Banking Companies Ordinance 1962")
    assert external.versions == []

    stub = db.query(RegDocument).filter(RegDocument.title == "Foreign Exchange Manual").one()
    assert stub.is_external == 0
    assert stub.versions == []
    assert stub.source_url.endswith("/laws-regulations/foreign-exchange-manual")


def test_sync_archives_content_and_extracts_text(fake_site, tmp_path):
    db = make_session()
    serve_all_pdfs(fake_site, listing_rows())

    laws.sync_laws(db, delay=0)

    version = (
        db.query(RegDocumentVersion)
        .join(RegDocument)
        .filter(RegDocument.title == "State Bank of Pakistan Act, 1956")
        .one()
    )
    assert version.file_type == "pdf"
    assert version.extraction_status == "extracted"
    assert version.content_text.startswith("%PDF")
    assert version.is_current == 1
    assert version.source == "live"
    # local_path is relative, as Attachment.local_path is.
    assert not Path(version.local_path).is_absolute()
    assert (tmp_path / version.local_path).exists()
    assert Path(version.local_path).name.startswith(version.content_hash[:8] + "-")


def test_unchanged_content_is_touched_not_duplicated(fake_site):
    db = make_session()
    serve_all_pdfs(fake_site, listing_rows())

    laws.sync_laws(db, delay=0)
    first = db.query(RegDocumentVersion).count()
    counts = laws.sync_laws(db, delay=0)

    assert counts["new_versions"] == 0
    assert counts["unchanged"] == 6
    assert db.query(RegDocumentVersion).count() == first


def test_replaced_content_becomes_a_new_version_and_history_is_kept(fake_site):
    """SBP replaces the PDF at the same URL; the old bytes stay ours forever."""
    db = make_session()
    rows = listing_rows()
    serve_all_pdfs(fake_site, rows)
    laws.sync_laws(db, delay=0)

    act = next(r for r in rows if r["title"] == "State Bank of Pakistan Act, 1956")
    fake_site.serve(act["url"], PDF_HEADER + b"amended 2026 text")
    laws.sync_laws(db, delay=0)

    document = db.query(RegDocument).filter(RegDocument.id == act["id"]).one()
    assert len(document.versions) == 2
    assert [v.is_current for v in document.versions] == [0, 1]
    assert document.current_version.content_text.endswith("amended 2026 text")

    superseded = next(v for v in document.versions if not v.is_current)
    assert superseded.content_text != document.current_version.content_text
    assert superseded.local_path is not None


def test_a_sync_error_does_not_abort_the_pass(fake_site):
    db = make_session()
    rows = listing_rows()
    serve_all_pdfs(fake_site, rows)
    act = next(r for r in rows if r["title"] == "State Bank of Pakistan Act, 1956")
    del fake_site.files[act["url"]]

    counts = laws.sync_laws(db, delay=0)

    assert counts["errors"] == 1
    assert counts["new_versions"] == 5
    assert db.query(RegDocument).count() == 10


def test_type_filter_and_limit_restrict_the_pass(fake_site):
    db = make_session()
    serve_all_pdfs(fake_site, listing_rows())

    counts = laws.sync_laws(db, doc_types=["law"], delay=0)
    assert counts["rows"] == 4
    assert {d.doc_type for d in db.query(RegDocument).all()} == {"law"}

    db2 = make_session()
    counts = laws.sync_laws(db2, limit=2, delay=0)
    assert counts["rows"] == 2
    assert db2.query(RegDocument).count() == 2


def test_delisting_only_happens_after_a_complete_pass(fake_site):
    db = make_session()
    serve_all_pdfs(fake_site, listing_rows())
    laws.sync_laws(db, delay=0)

    # A filtered pass sees a fraction of the listing and must not delist the rest.
    laws.sync_laws(db, doc_types=["law"], delay=0)
    assert db.query(RegDocument).filter(RegDocument.delisted_at.isnot(None)).count() == 0

    laws.sync_laws(db, limit=2, delay=0)
    assert db.query(RegDocument).filter(RegDocument.delisted_at.isnot(None)).count() == 0


def test_a_row_that_vanishes_is_delisted_and_keeps_its_content(fake_site, monkeypatch):
    db = make_session()
    rows = listing_rows()
    serve_all_pdfs(fake_site, rows)
    laws.sync_laws(db, delay=0)

    act = next(r for r in rows if r["title"] == "State Bank of Pakistan Act, 1956")
    monkeypatch.setattr(laws, "fetch_listing", lambda force=False: [
        r for r in listing_rows() if r["id"] != act["id"]
    ])
    counts = laws.sync_laws(db, delay=0)

    assert counts["delisted"] == 1
    document = db.query(RegDocument).filter(RegDocument.id == act["id"]).one()
    assert document.delisted_at is not None
    assert len(document.versions) == 1

    # ... and comes back to life if SBP relists it.
    monkeypatch.setattr(laws, "fetch_listing", lambda force=False: listing_rows())
    laws.sync_laws(db, delay=0)
    db.refresh(document)
    assert document.delisted_at is None


# ------------------------------------------------------------------ currency rule


def add_version(db, document_id, version_id, **overrides):
    fields = dict(
        id=version_id,
        document_id=document_id,
        content_hash=f"hash-{version_id}",
        file_type="pdf",
        source="live",
        is_current=0,
        first_seen_at=datetime(2024, 1, 1),
        last_seen_at=datetime(2024, 1, 1),
    )
    fields.update(overrides)
    version = RegDocumentVersion(**fields)
    db.add(version)
    return version


def test_future_edition_stays_pending_until_its_date_arrives():
    """Both SME PRs rows are live; only the one in force is current."""
    db = make_session()
    db.add(RegDocument(id="doc-1", title="PRs for SME Financing"))
    add_version(db, "doc-1", "oct-2024", version_label="updated till October 07, 2024")
    add_version(db, "doc-1", "jan-2026", effective_from=datetime(2026, 1, 1))
    db.commit()

    laws.select_current_versions(db, {"doc-1"}, now=datetime(2025, 6, 1))
    assert _current(db) == "oct-2024"

    # The date passes; no new content, but the pending edition takes force.
    laws.select_current_versions(db, {"doc-1"}, now=datetime(2026, 1, 2))
    assert _current(db) == "jan-2026"


def test_currency_ignores_fetch_order():
    db = make_session()
    db.add(RegDocument(id="doc-1", title="PRs for SME Financing"))
    add_version(db, "doc-1", "in-force", effective_from=datetime(2024, 10, 7))
    add_version(db, "doc-1", "pending", effective_from=datetime(2026, 1, 1))
    db.commit()

    rows_forward = {"doc-1": [
        {"version_id": "in-force", "order": 0, "listed_date": None},
        {"version_id": "pending", "order": 1, "listed_date": None},
    ]}
    rows_reversed = {"doc-1": [
        {"version_id": "pending", "order": 0, "listed_date": None},
        {"version_id": "in-force", "order": 1, "listed_date": None},
    ]}

    laws.select_current_versions(db, {"doc-1"}, rows_forward, now=datetime(2025, 6, 1))
    assert _current(db) == "in-force"
    laws.select_current_versions(db, {"doc-1"}, rows_reversed, now=datetime(2025, 6, 1))
    assert _current(db) == "in-force"


def test_without_effective_dates_the_listed_row_wins():
    db = make_session()
    db.add(RegDocument(id="doc-1", title="Guidelines for Clearing Operations"))
    add_version(db, "doc-1", "old", first_seen_at=datetime(2023, 1, 1))
    add_version(db, "doc-1", "listed", first_seen_at=datetime(2020, 1, 1))
    db.commit()

    laws.select_current_versions(
        db, {"doc-1"}, {"doc-1": [{"version_id": "listed", "order": 0, "listed_date": None}]}
    )
    assert _current(db) == "listed"


def test_with_nothing_observed_the_newest_capture_wins():
    db = make_session()
    db.add(RegDocument(id="doc-1", title="Guidelines for Clearing Operations"))
    add_version(db, "doc-1", "older", first_seen_at=datetime(2020, 1, 1))
    add_version(db, "doc-1", "newer", first_seen_at=datetime(2023, 1, 1))
    db.commit()

    laws.select_current_versions(db, {"doc-1"})
    assert _current(db) == "newer"


def test_backfilled_history_never_becomes_current():
    """Phase 8 inserts wayback captures; they are history, not the document in force."""
    db = make_session()
    db.add(RegDocument(id="doc-1", title="PRs for MFBs"))
    add_version(db, "doc-1", "live", first_seen_at=datetime(2020, 1, 1))
    add_version(db, "doc-1", "wayback", source="wayback", first_seen_at=datetime(2026, 1, 1))
    db.commit()

    laws.select_current_versions(db, {"doc-1"})
    assert _current(db) == "live"


def test_an_entirely_future_document_has_nothing_in_force():
    db = make_session()
    db.add(RegDocument(id="doc-1", title="Draft Regulations"))
    add_version(db, "doc-1", "future", effective_from=datetime(2027, 1, 1))
    db.commit()

    laws.select_current_versions(db, {"doc-1"}, now=datetime(2026, 8, 11))
    assert _current(db) is None


# ------------------------------------------------------- sync bookkeeping isolation


def test_laws_runs_stay_out_of_the_circular_sync_status(client):
    """A laws run is the newest SyncStatus row but must not become the circular banner."""
    import sbpeye.main as main_module
    from sbpeye.models import SyncStatus

    test_client, db_factory = client
    db = db_factory()
    try:
        db.add(SyncStatus(
            job_id="circ-1", status="success", kind="circulars",
            last_sync_date=datetime(2026, 8, 1), completed_at=datetime(2026, 8, 1),
        ))
        db.add(SyncStatus(
            job_id="laws-1", status="running", kind="laws",
            started_at=datetime(2026, 8, 11),
        ))
        db.commit()

        assert main_module._latest_sync_status(db).job_id == "circ-1"
        assert main_module._latest_successful_sync(db).job_id == "circ-1"
    finally:
        db.close()

    payload = test_client.get("/api/circulars/sync/status").json()
    assert payload["running"] is False


def test_legacy_rows_without_a_kind_still_count_as_circular_runs(client):
    """Rows predate the `kind` column; SQL's IN would silently drop their NULLs."""
    import sbpeye.main as main_module
    from sbpeye.models import SyncStatus

    _, db_factory = client
    db = db_factory()
    try:
        db.add(SyncStatus(job_id="legacy", status="success", last_sync_date=datetime(2026, 7, 1)))
        db.commit()

        assert main_module._latest_sync_status(db).job_id == "legacy"
        assert main_module._latest_successful_sync(db).job_id == "legacy"
    finally:
        db.close()


def _current(db) -> str | None:
    version = (
        db.query(RegDocumentVersion).filter(RegDocumentVersion.is_current == 1).one_or_none()
    )
    return version.id if version else None
