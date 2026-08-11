"""Phase 3 of the laws & regulations plan: subpages, children, recursion, manifests.

Fixtures are the `div.border-box` content container of real SBP subpages, captured
2026-08-11 — the Foreign Exchange Manual (two tables: numbered chapters and roman
appendices), its Appendix III (~36 notifications of inline HTML, no files), a card-layout
page, and a page with no key column at all.

See docs/LAWS_REGULATIONS_PLAN.md.
"""

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sbpeye.database import Base
from sbpeye.models import RegDocument, RegDocumentVersion
from sbpeye.scraper import laws

FIXTURES = Path(__file__).parent / "fixtures"
PDF_HEADER = b"%PDF-1.7\n"

FE_MANUAL_URL = "https://www.sbp.org.pk/laws-regulations/foreign-exchange-manual"
APPENDIX_III_URL = (
    "https://www.sbp.org.pk/laws-regulations/"
    "notifications-issued-by-the-state-bank-of-pakistan-under-foreign-exchange-"
    "regulation-act-1947-vii-of-1947"
)
CPIS_URL = (
    "https://www.sbp.org.pk/laws-regulations/"
    "guidelines-for-coordinated-portfolio-investment-survey-cpis"
)
MFS_URL = (
    "https://www.sbp.org.pk/laws-regulations/"
    "reporting-guides-monetary-and-financial-statistics"
)

PAGES = {
    FE_MANUAL_URL: "laws_subpage_fe_manual.html",
    APPENDIX_III_URL: "laws_subpage_fe_appendix_iii.html",
    CPIS_URL: "laws_subpage_cpis.html",
    MFS_URL: "laws_subpage_reporting_guides_mfs.html",
}


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def make_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False)()


def parts_of(url: str) -> list[dict]:
    return laws.parse_subpage(fixture(PAGES[url]), url)["parts"]


@pytest.fixture
def fake_site(monkeypatch, tmp_path):
    """Serves the fixture subpages, and a byte store for every child file."""

    class Site:
        def __init__(self):
            self.pages = dict(PAGES)
            self.files: dict[str, bytes] = {}
            self.fetched: list[str] = []

        def serve_parts(self, url: str, body: bytes = PDF_HEADER + b"v1"):
            """Give every downloadable part of a page distinct content."""
            for part in parts_of(url):
                if laws.route_link(part["url"]) == laws.ROUTE_PDF:
                    self.files[part["url"]] = body + part["url"].encode()

    site = Site()

    def fake_fetch_page_cached(url, force=False):
        site.fetched.append(url)
        name = site.pages.get(url)
        if name is None:
            raise AssertionError(f"unexpected page fetch: {url}")
        return fixture(name)

    def fake_download(document_id, url, force=False):
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


def make_container(document_id="fe-manual", url=FE_MANUAL_URL, **overrides) -> RegDocument:
    fields = dict(
        id=document_id,
        title="Foreign Exchange Manual",
        normalized_title="foreign exchange manual",
        doc_type="regulation",
        source_url=url,
        first_seen_at=datetime(2026, 8, 1),
        last_seen_at=datetime(2026, 8, 1),
    )
    fields.update(overrides)
    return RegDocument(**fields)


def sync_container(db, site, document=None, **kwargs):
    """Sync one container the way `sync_laws` does — including the currency pass.

    Currency is decided once per pass for every touched document, never inside the
    walk, so a standalone container sync has to close the pass itself.
    """
    document = document if document is not None else make_container()
    if db.query(RegDocument).filter(RegDocument.id == document.id).first() is None:
        db.add(document)
        db.commit()
    else:
        document = db.query(RegDocument).filter(RegDocument.id == document.id).one()
    now = kwargs.pop("now", datetime(2026, 8, 11))
    counts = {k: 0 for k in (
        "children", "manifests", "new_versions", "unchanged", "stubs", "delisted", "errors"
    )}
    touched: set[str] = set()
    observed: dict[str, list[dict]] = {}
    content_hash = laws.sync_subpage(
        db, document, now, counts, touched, set(), observed, delay=0, **kwargs,
    )
    laws.select_current_versions(db, touched, observed, now=now)
    db.commit()
    return counts, content_hash


# ------------------------------------------------------------------ subpage parsing


def test_fe_manual_parses_chapters_and_appendices():
    parts = parts_of(FE_MANUAL_URL)
    assert len(parts) == 26  # 22 chapters + 4 appendices

    chapters = [p for p in parts if p["part_label"].startswith("Chapter")]
    assert len(chapters) == 22
    assert chapters[11]["part_key"] == "12"
    assert chapters[11]["part_label"] == "Chapter 12"
    assert chapters[11]["title"] == "EXPORTS"
    assert chapters[11]["url"].endswith("/Chapter-12-foreign-exchange-manual.pdf")

    appendices = [p for p in parts if p["part_label"].startswith("Appendix")]
    assert [p["part_key"] for p in appendices] == ["I", "II", "III", "IV"]


def test_part_noun_comes_from_the_tables_own_wording():
    """"CHAPTERS" heads one table; the other is only "Sr. No." under a caption."""
    parts = {p["part_key"]: p["part_label"] for p in parts_of(FE_MANUAL_URL)}
    assert parts["1"] == "Chapter 1"
    assert parts["IV"] == "Appendix IV"


def test_a_row_that_names_itself_overrides_the_column_header():
    """SBP files annexures in a column headed "Chapter No."; the row wins."""
    assert laws._title_noun("Annexure - III") == "Annexure"
    assert laws._title_noun("EXPORTS") is None


def test_appendix_iii_is_a_document_not_a_container():
    """Depth 3 of the hierarchy, and its content is the page itself — no files."""
    parsed = laws.parse_subpage(fixture(PAGES[APPENDIX_III_URL]), APPENDIX_III_URL)
    assert parsed["parts"] == []
    assert len(parsed["content_text"]) > 5000
    assert "APPENDIX III" in parsed["content_text"]


def test_prose_tables_never_become_parts():
    """Appendix III has six tables of its own; none of them lists a document."""
    from bs4 import BeautifulSoup

    root = BeautifulSoup(fixture(PAGES[APPENDIX_III_URL]), "html.parser")
    assert len(root.find_all("table")) >= 4
    assert laws.parse_subpage(fixture(PAGES[APPENDIX_III_URL]), APPENDIX_III_URL)["parts"] == []


def test_pages_without_a_key_column_key_parts_by_title():
    parts = parts_of(CPIS_URL)
    assert [p["part_key"] for p in parts] == [
        "covering letter", "cpis forms i", "cpis forms ii", "guidelines",
    ]
    assert parts[0]["part_label"] == "Covering Letter"


def test_card_layouts_take_the_title_from_the_card_not_the_link():
    """Every card's link reads "Download Document"."""
    parts = parts_of(MFS_URL)
    assert len(parts) == 8
    assert parts[0]["part_label"] == "Financial Auxiliaries"
    assert parts[0]["url"].endswith("/Financial_Guide-1.pdf")
    assert all("Download" not in p["part_label"] for p in parts)


def test_child_files_may_live_anywhere_on_sbp():
    """Chapters sit in /assets/document/, a third store distinct from the laws folder."""
    urls = [p["url"] for p in parts_of(FE_MANUAL_URL)]
    assert any("/assets/document/" in url for url in urls)
    assert all(laws.route_link(url) in (laws.ROUTE_PDF, laws.ROUTE_SUBPAGE) for url in urls)


def test_child_identity_is_keyed_on_the_part_not_the_title():
    """A re-worded subject line must not fork Chapter 13 into a second document."""
    assert laws.child_identity("foreign-exchange-manual", "13") == laws.child_identity(
        "foreign-exchange-manual", "13"
    )
    assert laws.child_identity("foreign-exchange-manual", "13") != laws.child_identity(
        "reporting-guidelines", "13"
    )


# -------------------------------------------------------------------- container sync


def test_sync_captures_every_part_of_the_fe_manual(fake_site):
    db = make_session()
    fake_site.serve_parts(FE_MANUAL_URL)
    fake_site.serve_parts(APPENDIX_III_URL)

    counts, _ = sync_container(db, fake_site)

    assert counts["children"] == 26
    assert counts["errors"] == 0

    manual = db.query(RegDocument).filter(RegDocument.id == "fe-manual").one()
    assert manual.page_slug == "foreign-exchange-manual"
    assert len(manual.children) == 26
    assert [c.part_order for c in manual.children] == list(range(26))

    chapter_12 = next(c for c in manual.children if c.part_label == "Chapter 12")
    assert chapter_12.parent_id == "fe-manual"
    assert chapter_12.doc_type == "regulation"   # inherited from the container
    assert chapter_12.current_version.file_type == "pdf"


def test_chapters_revise_independently(fake_site):
    """The point of per-part documents: one chapter changing is one new version."""
    db = make_session()
    fake_site.serve_parts(FE_MANUAL_URL)
    sync_container(db, fake_site)

    chapter_12 = next(
        c for c in db.query(RegDocument).filter(RegDocument.parent_id == "fe-manual")
        if c.part_label == "Chapter 12"
    )
    fake_site.files[chapter_12.source_url] = PDF_HEADER + b"chapter 12, revised 2026"
    counts, _ = sync_container(db, fake_site)

    assert counts["new_versions"] == 1
    assert counts["unchanged"] == 25
    db.refresh(chapter_12)
    assert len(chapter_12.versions) == 2
    assert chapter_12.current_version.content_text.endswith("chapter 12, revised 2026")

    others = db.query(RegDocument).filter(
        RegDocument.parent_id == "fe-manual", RegDocument.id != chapter_12.id
    ).all()
    assert all(len(c.versions) <= 1 for c in others)


def test_recursion_reaches_appendix_iii_as_html_content(fake_site):
    db = make_session()
    fake_site.serve_parts(FE_MANUAL_URL)

    sync_container(db, fake_site)

    appendix = db.query(RegDocument).filter(RegDocument.part_label == "Appendix III").one()
    assert appendix.parent_id == "fe-manual"
    assert appendix.page_slug is not None

    version = appendix.current_version
    assert version.file_type == "html"
    assert version.file_url is None
    assert version.local_path is None
    assert "APPENDIX III" in version.content_text
    assert version.content_hash == hashlib.sha256(
        version.content_text.encode("utf-8")
    ).hexdigest()


def test_html_content_hashes_the_text_not_the_markup(fake_site):
    """A template tweak around the content must not read as a new edition."""
    db = make_session()
    document = make_container("appendix-iii", APPENDIX_III_URL, title="Appendix III")
    db.add(document)
    db.commit()

    sync_container(db, fake_site, document)
    original = document.current_version.content_hash

    restyled = fixture(PAGES[APPENDIX_III_URL]).replace(
        b'class="border-box"', b'class="border-box shadow-sm rounded"'
    )
    fake_site.pages[APPENDIX_III_URL] = "laws_subpage_fe_appendix_iii_restyled.html"
    (FIXTURES / "laws_subpage_fe_appendix_iii_restyled.html").write_bytes(restyled)
    try:
        counts, _ = sync_container(db, fake_site, document)
        assert counts["new_versions"] == 0
        assert counts["unchanged"] == 1
        assert document.current_version.content_hash == original
    finally:
        (FIXTURES / "laws_subpage_fe_appendix_iii_restyled.html").unlink()


def test_a_cycle_between_pages_terminates(fake_site, monkeypatch):
    """A subpage linking back to its own parent must not recurse forever."""
    db = make_session()
    self_referential = (
        b'<div class="border-box"><table>'
        b'<tr><th>Chapters</th><th>Subject</th></tr>'
        b'<tr><td>1</td><td>Loop</td><td><a href="' + FE_MANUAL_URL.encode() + b'">x</a></td></tr>'
        b"</table></div>"
    )
    path = FIXTURES / "laws_subpage_cycle.html"
    path.write_bytes(self_referential)
    fake_site.pages[FE_MANUAL_URL] = "laws_subpage_cycle.html"
    try:
        counts, _ = sync_container(db, fake_site)
        assert counts["errors"] == 0
        assert fake_site.fetched.count(FE_MANUAL_URL) == 1
    finally:
        path.unlink()


def test_a_failed_part_does_not_lose_the_container(fake_site):
    db = make_session()
    fake_site.serve_parts(FE_MANUAL_URL)
    broken = parts_of(FE_MANUAL_URL)[0]["url"]
    del fake_site.files[broken]

    counts, _ = sync_container(db, fake_site)

    assert counts["errors"] == 1
    assert counts["children"] == 26
    assert db.query(RegDocument).filter(RegDocument.parent_id == "fe-manual").count() == 26


def test_a_part_that_disappears_is_delisted_not_deleted(fake_site):
    db = make_session()
    fake_site.serve_parts(CPIS_URL)
    document = make_container("cpis", CPIS_URL, title="CPIS Guidelines")
    db.add(document)
    db.commit()
    sync_container(db, fake_site, document)
    assert db.query(RegDocument).filter(RegDocument.parent_id == "cpis").count() == 4

    trimmed = fixture(PAGES[CPIS_URL]).replace(b"Covering Letter", b"REMOVED", 1)
    path = FIXTURES / "laws_subpage_cpis_trimmed.html"
    path.write_bytes(trimmed)
    fake_site.pages[CPIS_URL] = "laws_subpage_cpis_trimmed.html"
    try:
        counts, _ = sync_container(db, fake_site, document)
        assert counts["delisted"] == 1
        gone = db.query(RegDocument).filter(
            RegDocument.parent_id == "cpis", RegDocument.delisted_at.isnot(None)
        ).one()
        assert gone.part_label == "Covering Letter"
        assert db.query(RegDocument).filter(RegDocument.parent_id == "cpis").count() == 5
    finally:
        path.unlink()


# ------------------------------------------------------------------------- manifests


def test_the_container_version_is_a_manifest_of_its_parts(fake_site):
    db = make_session()
    fake_site.serve_parts(FE_MANUAL_URL)
    fake_site.serve_parts(APPENDIX_III_URL)

    counts, content_hash = sync_container(db, fake_site)

    assert counts["manifests"] == 1
    manual = db.query(RegDocument).filter(RegDocument.id == "fe-manual").one()
    version = manual.current_version
    assert version.file_type == "manifest"
    assert version.file_url is None
    assert version.content_hash == content_hash

    manifest = json.loads(version.content_text)
    assert len(manifest["parts"]) == 26
    assert manifest["parts"][11]["part_label"] == "Chapter 12"
    assert all(entry["content_hash"] for entry in manifest["parts"])

    children = {c.id: c for c in manual.children}
    for entry in manifest["parts"]:
        assert entry["content_hash"] == children[entry["id"]].current_version.content_hash


def test_a_manifest_appears_only_when_a_part_actually_changed(fake_site):
    db = make_session()
    fake_site.serve_parts(FE_MANUAL_URL)
    sync_container(db, fake_site)

    counts, _ = sync_container(db, fake_site)
    assert counts["manifests"] == 0
    manual = db.query(RegDocument).filter(RegDocument.id == "fe-manual").one()
    assert len(manual.versions) == 1

    chapter_1 = next(c for c in manual.children if c.part_label == "Chapter 1")
    fake_site.files[chapter_1.source_url] = PDF_HEADER + b"revised"
    counts, _ = sync_container(db, fake_site)

    assert counts["manifests"] == 1
    db.refresh(manual)
    assert len(manual.versions) == 2


def test_two_manifests_say_which_part_moved(fake_site):
    """The manifest history doubles as the change log."""
    db = make_session()
    fake_site.serve_parts(FE_MANUAL_URL)
    sync_container(db, fake_site)
    manual = db.query(RegDocument).filter(RegDocument.id == "fe-manual").one()
    chapter_20 = next(c for c in manual.children if c.part_label == "Chapter 20")
    fake_site.files[chapter_20.source_url] = PDF_HEADER + b"securities, revised"
    sync_container(db, fake_site)

    db.refresh(manual)
    before, after = sorted(manual.versions, key=lambda v: v.first_seen_at)
    hashes_before = {p["id"]: p["content_hash"] for p in json.loads(before.content_text)["parts"]}
    hashes_after = {p["id"]: p["content_hash"] for p in json.loads(after.content_text)["parts"]}
    changed = [k for k in hashes_after if hashes_after[k] != hashes_before.get(k)]

    assert changed == [chapter_20.id]


def test_manifests_are_flagged_so_search_can_skip_them(fake_site):
    """A manifest is bookkeeping; phase 5 must not index it as readable text."""
    db = make_session()
    fake_site.serve_parts(CPIS_URL)
    document = make_container("cpis", CPIS_URL, title="CPIS Guidelines")
    db.add(document)
    db.commit()
    sync_container(db, fake_site, document)

    kinds = {v.file_type for v in db.query(RegDocumentVersion).all()}
    assert "manifest" in kinds
    manifest = db.query(RegDocumentVersion).filter(
        RegDocumentVersion.file_type == "manifest"
    ).one()
    assert json.loads(manifest.content_text)["document_id"] == "cpis"


# ----------------------------------------------------------------- full-listing wiring


def test_listing_sync_follows_subpages(monkeypatch, tmp_path):
    """`sbpeye laws sync` now walks into containers instead of leaving stubs."""
    db = make_session()
    listing_row = {
        "title": "Foreign Exchange Manual",
        "normalized_title": "foreign exchange manual",
        "id": laws.law_identity("Foreign Exchange Manual"),
        "doc_type": "regulation",
        "url": FE_MANUAL_URL,
        "route": laws.ROUTE_SUBPAGE,
        "listed_date": None,
        "version_label": None,
        "effective_from": None,
        "order": 0,
    }
    monkeypatch.setattr(laws, "fetch_listing", lambda force=False: [dict(listing_row)])
    monkeypatch.setattr(laws, "fetch_page_cached", lambda url, force=False: fixture(PAGES[url]))
    monkeypatch.setattr(laws, "PROJECT_ROOT", tmp_path)

    def fake_download(document_id, url, force=False):
        body = PDF_HEADER + url.encode()
        content_hash = hashlib.sha256(body).hexdigest()
        destination = tmp_path / document_id / laws._archive_name(content_hash, url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)
        return destination, content_hash, None

    monkeypatch.setattr(laws, "download_law_file", fake_download)
    monkeypatch.setattr(
        laws, "extract_document_text",
        lambda path, file_type: ("text", "extracted", None),
    )

    counts = laws.sync_laws(db, delay=0)

    assert counts["documents"] == 1
    assert counts["children"] == 26
    assert counts["manifests"] == 1
    assert counts["delisted"] == 0     # children are not listing rows
    assert db.query(RegDocument).count() == 27

    # Every document, container and part alike, ends the pass with a decided currency.
    for document in db.query(RegDocument).all():
        if document.versions:
            assert sum(v.is_current for v in document.versions) == 1


def test_skip_subpages_leaves_containers_as_stubs(monkeypatch, tmp_path):
    db = make_session()
    listing_row = {
        "title": "Foreign Exchange Manual",
        "normalized_title": "foreign exchange manual",
        "id": laws.law_identity("Foreign Exchange Manual"),
        "doc_type": "regulation",
        "url": FE_MANUAL_URL,
        "route": laws.ROUTE_SUBPAGE,
        "listed_date": None,
        "version_label": None,
        "effective_from": None,
        "order": 0,
    }
    monkeypatch.setattr(laws, "fetch_listing", lambda force=False: [dict(listing_row)])

    counts = laws.sync_laws(db, delay=0, skip_subpages=True)

    assert counts["stubs"] == 1
    assert counts["children"] == 0
    assert db.query(RegDocument).count() == 1
