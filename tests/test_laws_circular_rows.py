"""Phase 4 of the laws & regulations plan: listing rows that are circulars.

Several Guidelines in SBP's laws listing link to `/circulars/<slug>` pages SBPEye already
holds in full. They must resolve to the existing `Circular` rather than being scraped a
second time into a record that drifts away from it.

See docs/LAWS_REGULATIONS_PLAN.md.
"""

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sbpeye.database import Base
from sbpeye.link_routing import find_circular_by_url
from sbpeye.models import Circular, RegDocument, RegDocumentLink, RegDocumentVersion
from sbpeye.scraper import laws

FIXTURE = Path(__file__).parent / "fixtures" / "laws_listing.html"

IBD_05_2007 = "https://www.sbp.org.pk/circulars/ibd-circular-no-05-of-2007"
BPRD_04_2016 = "https://www.sbp.org.pk/circulars/bprd-circular-no-04-of-2016"


def make_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False)()


def make_circular(circular_id="circ-1", reference="IBD Circular No. 05 of 2007",
                  url=IBD_05_2007, **overrides) -> Circular:
    fields = dict(
        id=circular_id,
        reference=reference,
        title="Guidelines for Islamic Microfinance Business",
        department="IBD",
        date=datetime(2007, 11, 1),
        url=url,
        new_url=url,
        content_text="The full text of the circular, already indexed.",
    )
    fields.update(overrides)
    return Circular(**fields)


def make_document(document_id="doc-1", url=IBD_05_2007, **overrides) -> RegDocument:
    fields = dict(
        id=document_id,
        title="Guidelines for Islamic Microfinance Business",
        normalized_title="guidelines for islamic microfinance business",
        doc_type="guideline",
        source_url=url,
        first_seen_at=datetime(2026, 8, 11),
        last_seen_at=datetime(2026, 8, 11),
    )
    fields.update(overrides)
    return RegDocument(**fields)


# ------------------------------------------------------------------------- url lookup


def test_a_circular_resolves_by_any_of_its_three_urls():
    db = make_session()
    db.add(make_circular(
        url="https://www.sbp.org.pk/circulars/ibd-circular-no-05-of-2007",
        new_url="https://www.sbp.org.pk/circulars/ibd-circular-no-05-of-2007",
        old_url="https://www.sbp.org.pk/ibd/2007/C5.htm",
    ))
    db.commit()

    for url in (
        "https://www.sbp.org.pk/circulars/ibd-circular-no-05-of-2007",
        "https://www.sbp.org.pk/ibd/2007/C5.htm",
        "https://www.sbp.org.pk/CIRCULARS/IBD-Circular-No-05-of-2007",  # SBP's own casing varies
        "https://www.sbp.org.pk/circulars/ibd-circular-no-05-of-2007#top",
    ):
        assert find_circular_by_url(db, url) is not None, url


def test_url_lookup_rejects_what_is_not_an_sbp_circular():
    db = make_session()
    db.add(make_circular())
    db.commit()

    assert find_circular_by_url(db, None) is None
    assert find_circular_by_url(db, "https://example.com/circulars/x") is None
    assert find_circular_by_url(db, "https://www.sbp.org.pk/circulars/not-indexed") is None


# --------------------------------------------------------------------------- resolving


def test_a_circular_typed_row_points_at_the_circular_and_stores_no_content():
    db = make_session()
    db.add(make_circular())
    document = make_document()
    db.add(document)
    db.commit()

    assert laws.resolve_circular_row(db, document) is True
    db.commit()

    assert document.circular_id == "circ-1"
    assert document.versions == []
    assert document.circular.content_text.startswith("The full text")

    link = db.query(RegDocumentLink).one()
    assert (link.circular_id, link.document_id) == ("circ-1", "doc-1")
    assert link.link_type == "listing"
    assert link.detected_via == "listing"


def test_an_unindexed_circular_leaves_a_stub_to_retry():
    """Circular sync may not have reached it yet; the row waits rather than duplicating."""
    db = make_session()
    document = make_document(url="https://www.sbp.org.pk/circulars/ibd-circular-no-01-of-2017")
    db.add(document)
    db.commit()

    assert laws.resolve_circular_row(db, document) is False
    db.commit()

    assert document.circular_id is None
    assert db.query(RegDocumentLink).count() == 0

    # The circular arrives on a later circular sync; the next laws sync picks it up.
    db.add(make_circular(
        "circ-2",
        reference="IBD Circular No. 01 of 2017",
        url="https://www.sbp.org.pk/circulars/ibd-circular-no-01-of-2017",
    ))
    db.commit()

    assert laws.resolve_circular_row(db, document) is True
    assert document.circular_id == "circ-2"


def test_resolving_twice_does_not_duplicate_the_link():
    db = make_session()
    db.add(make_circular())
    document = make_document()
    db.add(document)
    db.commit()

    laws.resolve_circular_row(db, document)
    laws.resolve_circular_row(db, document)
    db.commit()

    assert db.query(RegDocumentLink).count() == 1


def test_the_link_is_navigable_from_the_circular_side():
    db = make_session()
    circular = make_circular()
    db.add(circular)
    document = make_document()
    db.add(document)
    db.commit()

    laws.resolve_circular_row(db, document)
    db.commit()

    db.refresh(circular)
    assert [link.document.title for link in circular.reg_links] == [
        "Guidelines for Islamic Microfinance Business"
    ]


# ----------------------------------------------------------------------- sync wiring


@pytest.fixture
def listing_site(monkeypatch, tmp_path):
    """The phase-2 listing fixture with downloads stubbed out."""
    monkeypatch.setattr(laws, "fetch_page_cached", lambda url, force=False: FIXTURE.read_bytes())
    monkeypatch.setattr(laws, "PROJECT_ROOT", tmp_path)

    def fake_download(document_id, url, force=False):
        destination = tmp_path / document_id / "file.pdf"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"%PDF-1.7\n" + url.encode())
        return destination, f"hash-{abs(hash(url))}", None

    monkeypatch.setattr(laws, "download_law_file", fake_download)
    monkeypatch.setattr(
        laws, "extract_document_text", lambda path, file_type: ("text", "extracted", None)
    )


def test_sync_resolves_the_circular_rows_it_can(listing_site):
    """The fixture listing carries two circular-typed rows; only one is indexed."""
    db = make_session()
    db.add(make_circular("circ-known", url=BPRD_04_2016, new_url=BPRD_04_2016,
                         reference="BPRD Circular No. 04 of 2016", title="Branch Licensing Policy"))
    db.commit()

    counts = laws.sync_laws(db, delay=0, skip_subpages=True)

    assert counts["resolved"] == 1
    assert counts["stubs"] == 2   # the unindexed circular row and the FE Manual subpage

    resolved = db.query(RegDocument).filter(RegDocument.circular_id.isnot(None)).one()
    assert resolved.title == "Branch Licensing Policy"
    assert resolved.versions == []
    assert db.query(RegDocumentLink).count() == 1


def test_a_row_that_is_a_circular_is_never_downloaded(listing_site):
    """No content on the RegDocument side — that is the whole point of resolving."""
    db = make_session()
    db.add(make_circular("circ-known", url=BPRD_04_2016, new_url=BPRD_04_2016))
    db.commit()

    laws.sync_laws(db, delay=0, skip_subpages=True)

    circular_docs = db.query(RegDocument).filter(RegDocument.circular_id.isnot(None)).all()
    for document in circular_docs:
        assert db.query(RegDocumentVersion).filter(
            RegDocumentVersion.document_id == document.id
        ).count() == 0


def test_resolution_is_idempotent_across_syncs(listing_site):
    db = make_session()
    db.add(make_circular("circ-known", url=BPRD_04_2016, new_url=BPRD_04_2016))
    db.commit()

    laws.sync_laws(db, delay=0, skip_subpages=True)
    laws.sync_laws(db, delay=0, skip_subpages=True)

    assert db.query(RegDocumentLink).count() == 1
    assert db.query(RegDocument).filter(RegDocument.circular_id.isnot(None)).count() == 1


def test_a_stub_resolves_on_a_later_sync_once_the_circular_lands(listing_site):
    db = make_session()
    counts = laws.sync_laws(db, delay=0, skip_subpages=True)
    assert counts["resolved"] == 0
    assert counts["stubs"] == 3

    db.add(make_circular("circ-known", url=BPRD_04_2016, new_url=BPRD_04_2016))
    db.commit()

    counts = laws.sync_laws(db, delay=0, skip_subpages=True)
    assert counts["resolved"] == 1
    assert counts["stubs"] == 2
