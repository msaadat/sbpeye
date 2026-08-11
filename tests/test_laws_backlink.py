"""Phase 6a of the laws & regulations plan: deterministic circular ↔ regulation links.

No LLM: every edge asserted here comes from a URL that resolves to a document we hold, or
from a document's name appearing in a circular's text. Judgements about *meaning*
(amends vs clarifies) belong to phase 6b and are deliberately absent.

See docs/LAWS_REGULATIONS_PLAN.md.
"""

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sbpeye import laws_links
from sbpeye.database import Base
from sbpeye.models import Attachment, Circular, RegDocument, RegDocumentLink, RegDocumentVersion

FE_MANUAL_URL = "https://www.sbp.org.pk/laws-regulations/foreign-exchange-manual"
CHAPTER_12_URL = "https://www.sbp.org.pk/assets/document/Chapter-12-foreign-exchange-manual.pdf"
SME_PDF_URL = "https://www.sbp.org.pk/assets/documents/laws_regulations/PRs-SME.pdf"


def make_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False)()


def add_document(db, document_id, title, source_url=None, file_url=None, **kwargs):
    document = RegDocument(
        id=document_id,
        title=title,
        normalized_title=title.casefold(),
        doc_type=kwargs.pop("doc_type", "regulation"),
        source_url=source_url,
        first_seen_at=datetime(2026, 8, 1),
        **kwargs,
    )
    db.add(document)
    if file_url:
        db.add(RegDocumentVersion(
            id=f"{document_id}-v1",
            document_id=document_id,
            content_hash=f"hash-{document_id}",
            file_url=file_url,
            file_type="pdf",
            is_current=1,
            first_seen_at=datetime(2026, 8, 1),
        ))
    db.commit()
    return document


def add_circular(db, circular_id, text, reference="BPRD Circular No. 01 of 2026"):
    circular = Circular(
        id=circular_id,
        reference=reference,
        title="Test circular",
        department="BPRD",
        date=datetime(2026, 1, 1),
        url=f"https://www.sbp.org.pk/circulars/{circular_id}",
        content_text=text,
    )
    db.add(circular)
    db.commit()
    return circular


def corpus(db):
    """A small stand-in for the real listing, including a container with parts."""
    add_document(db, "fe-manual", "Foreign Exchange Manual", source_url=FE_MANUAL_URL)
    add_document(db, "fe-ch-12", "EXPORTS", file_url=CHAPTER_12_URL,
                 parent_id="fe-manual", part_label="Chapter 12", part_order=12)
    add_document(db, "fe-ch-13", "IMPORTS", parent_id="fe-manual",
                 part_label="Chapter 13", part_order=13)
    add_document(db, "sme-prs", "Prudential Regulations for SME Financing",
                 source_url=SME_PDF_URL, file_url=SME_PDF_URL)
    add_document(db, "reporting", "Reporting Guidelines",
                 source_url="https://www.sbp.org.pk/laws-regulations/reporting-guidelines")
    add_document(db, "external-law", "Banking Companies Ordinance 1962",
                 source_url="https://pakistancode.gov.pk/english/xyz", is_external=1)


# ----------------------------------------------------------------------- url matching


def test_a_url_in_circular_text_links_to_its_document():
    db = make_session()
    corpus(db)
    circular = add_circular(
        db, "circ-1",
        f"Authorized dealers may refer to the manual at {FE_MANUAL_URL} for details.",
    )

    counts = laws_links.link_circular_to_laws(db, circular)
    db.commit()

    assert counts["url_scan"] == 1
    link = db.query(RegDocumentLink).one()
    assert (link.document_id, link.detected_via, link.link_type) == (
        "fe-manual", "url_scan", "references",
    )


@pytest.mark.parametrize("written", [
    f"{FE_MANUAL_URL}.",                 # sentence punctuation glued on
    f"{FE_MANUAL_URL}/",                 # trailing slash
    f"{FE_MANUAL_URL}#Regulations",      # fragment
    FE_MANUAL_URL.upper(),               # SBP's casing varies
    f"({FE_MANUAL_URL})",                # parenthesised
])
def test_url_matching_survives_how_circulars_write_urls(written):
    db = make_session()
    corpus(db)
    circular = add_circular(db, "circ-1", f"See {written} for the current text.")

    laws_links.link_circular_to_laws(db, circular)
    db.commit()

    assert db.query(RegDocumentLink).count() == 1


def test_the_bare_listing_url_identifies_no_document():
    db = make_session()
    corpus(db)
    circular = add_circular(
        db, "circ-1", "Refer to https://www.sbp.org.pk/laws-regulations for all laws."
    )

    laws_links.link_circular_to_laws(db, circular)
    db.commit()

    assert db.query(RegDocumentLink).count() == 0


def test_a_shared_asset_store_url_that_is_not_a_law_creates_nothing():
    """`/assets/document/` also holds circular annexures; only known URLs count."""
    db = make_session()
    corpus(db)
    circular = add_circular(
        db, "circ-1",
        "The annexure is at https://www.sbp.org.pk/assets/document/FEC1-Annex-B.pdf here.",
    )

    laws_links.link_circular_to_laws(db, circular)
    db.commit()

    assert db.query(RegDocumentLink).count() == 0


def test_an_attachment_url_links_the_circular_that_carries_it():
    db = make_session()
    corpus(db)
    circular = add_circular(db, "circ-1", "See the attached regulations.")
    db.add(Attachment(
        id="att-1", circular_id="circ-1", filename="PRs-SME.pdf",
        original_url=SME_PDF_URL, file_type="pdf",
    ))
    db.commit()

    counts = laws_links.link_circular_to_laws(db, circular)
    db.commit()

    assert counts["url_scan"] == 1
    assert db.query(RegDocumentLink).one().document_id == "sme-prs"


# ---------------------------------------------------------------------- name matching


def test_a_document_named_in_the_text_is_linked():
    db = make_session()
    corpus(db)
    circular = add_circular(
        db, "circ-1",
        "Banks are advised that the Prudential Regulations for SME Financing are revised.",
    )

    counts = laws_links.link_circular_to_laws(db, circular)
    db.commit()

    assert counts["name_match"] == 1
    link = db.query(RegDocumentLink).one()
    assert (link.document_id, link.detected_via) == ("sme-prs", "name_match")


def test_name_matching_ignores_punctuation_and_case():
    db = make_session()
    corpus(db)
    add_document(db, "cb-rules", "Credit Bureau Rules, 2016")
    circular = add_circular(
        db, "circ-1", "issued under the CREDIT BUREAU RULES 2016 as amended"
    )

    laws_links.link_circular_to_laws(db, circular)
    db.commit()

    assert db.query(RegDocumentLink).one().document_id == "cb-rules"


def test_an_initialism_is_recognised():
    """SBP cites the Foreign Exchange Manual as "FE Manual" more often than in full."""
    db = make_session()
    corpus(db)
    circular = add_circular(db, "circ-1", "as provided in the FE Manual, paragraph 4")

    laws_links.link_circular_to_laws(db, circular)
    db.commit()

    assert db.query(RegDocumentLink).one().document_id == "fe-manual"


def test_a_part_reference_links_the_part_not_the_container():
    """The chapter is the document that would change, so the link points at it."""
    db = make_session()
    corpus(db)
    circular = add_circular(
        db, "circ-1",
        "In terms of Chapter 12 of the Foreign Exchange Manual, exporters shall...",
    )

    laws_links.link_circular_to_laws(db, circular)
    db.commit()

    links = db.query(RegDocumentLink).all()
    assert [link.document_id for link in links] == ["fe-ch-12"]


def test_several_parts_of_one_container_each_get_a_link():
    db = make_session()
    corpus(db)
    circular = add_circular(
        db, "circ-1",
        "Chapter 12 and Chapter 13 of the Foreign Exchange Manual are amended.",
    )

    laws_links.link_circular_to_laws(db, circular)
    db.commit()

    assert {link.document_id for link in db.query(RegDocumentLink)} == {"fe-ch-12", "fe-ch-13"}


def test_a_far_away_chapter_number_is_not_pulled_in():
    db = make_session()
    corpus(db)
    circular = add_circular(
        db, "circ-1",
        "The Foreign Exchange Manual governs this. " + ("filler text. " * 40)
        + "Separately, Chapter 13 of the Companies Act applies.",
    )

    laws_links.link_circular_to_laws(db, circular)
    db.commit()

    assert [link.document_id for link in db.query(RegDocumentLink)] == ["fe-manual"]


def test_titles_too_generic_to_identify_a_document_are_never_matched():
    """"Reporting Guidelines" is a phrase, not a name."""
    db = make_session()
    corpus(db)
    circular = add_circular(
        db, "circ-1", "Banks shall follow the reporting guidelines issued by the department."
    )

    laws_links.link_circular_to_laws(db, circular)
    db.commit()

    assert db.query(RegDocumentLink).count() == 0


def test_part_subject_lines_are_never_matched_by_name():
    """A part's title is a subject line — "EXPORTS" would match half the corpus."""
    db = make_session()
    corpus(db)
    circular = add_circular(db, "circ-1", "This circular concerns exports and imports.")

    laws_links.link_circular_to_laws(db, circular)
    db.commit()

    assert db.query(RegDocumentLink).count() == 0


def test_externally_hosted_laws_are_still_name_matched():
    """The most-cited statute in the corpus is one we hold no text for.

    Skipping it would leave a law that appears in the listing and in no circular's link
    list; `is_external` is what tells a consumer the text lives off-site.
    """
    db = make_session()
    corpus(db)
    circular = add_circular(
        db, "circ-1", "under the Banking Companies Ordinance 1962, section 41"
    )

    laws_links.link_circular_to_laws(db, circular)
    db.commit()

    link = db.query(RegDocumentLink).one()
    assert link.document_id == "external-law"
    assert link.detected_via == "name_match"


def test_a_url_match_wins_over_a_name_match_for_the_same_document():
    db = make_session()
    corpus(db)
    circular = add_circular(
        db, "circ-1",
        f"The Prudential Regulations for SME Financing are published at {SME_PDF_URL}.",
    )

    counts = laws_links.link_circular_to_laws(db, circular)
    db.commit()

    assert (counts["url_scan"], counts["name_match"]) == (1, 0)
    assert db.query(RegDocumentLink).count() == 1


def test_links_are_recorded_as_references_never_as_amends():
    """Whether a circular *amends* a regulation is a judgement no rule here makes."""
    db = make_session()
    corpus(db)
    circular = add_circular(
        db, "circ-1",
        "The Prudential Regulations for SME Financing are hereby amended with effect from...",
    )

    laws_links.link_circular_to_laws(db, circular)
    db.commit()

    assert {link.link_type for link in db.query(RegDocumentLink)} == {"references"}


# --------------------------------------------------------------------- backfill pass


def test_backlink_scans_the_corpus_and_is_idempotent():
    db = make_session()
    corpus(db)
    add_circular(db, "circ-1", f"See {FE_MANUAL_URL} for details.")
    add_circular(db, "circ-2", "The Prudential Regulations for SME Financing apply.")
    add_circular(db, "circ-3", "Nothing relevant here.")

    totals = laws_links.backlink_circulars(db)
    assert (totals["scanned"], totals["linked"]) == (3, 2)
    assert (totals["url_scan"], totals["name_match"]) == (1, 1)
    assert db.query(RegDocumentLink).count() == 2

    again = laws_links.backlink_circulars(db)
    assert again["scanned"] == 1          # only the circular with no links is re-checked
    assert db.query(RegDocumentLink).count() == 2

    rescan = laws_links.backlink_circulars(db, rescan=True)
    assert rescan["scanned"] == 3
    assert db.query(RegDocumentLink).count() == 2


def test_backlink_leaves_the_refetch_flag_alone_by_default():
    """3,600 old circulars naming a regulation is not news; a new one is."""
    db = make_session()
    corpus(db)
    add_circular(db, "circ-1", f"See {FE_MANUAL_URL}.")

    laws_links.backlink_circulars(db)

    assert db.query(RegDocument).filter(RegDocument.refetch_requested == 1).count() == 0

    laws_links.backlink_circulars(db, rescan=True, request_refetch=True)
    assert db.query(RegDocument).filter(RegDocument.refetch_requested == 1).count() == 1


def test_the_listing_link_from_phase_4_is_not_disturbed():
    db = make_session()
    corpus(db)
    circular = add_circular(db, "circ-1", f"See {FE_MANUAL_URL}.")
    db.add(RegDocumentLink(circular_id="circ-1", document_id="fe-manual",
                           link_type="listing", detected_via="listing"))
    db.commit()

    laws_links.link_circular_to_laws(db, circular)
    db.commit()

    types = {link.link_type for link in db.query(RegDocumentLink)}
    assert types == {"listing", "references"}


# ------------------------------------------------------------------- refetch trigger


def test_a_flagged_document_is_synced_by_a_filtered_pass(monkeypatch, tmp_path):
    """The trigger's whole purpose: a narrow sync still looks at what just changed."""
    from sbpeye.scraper import laws

    db = make_session()
    rows = [
        {"title": "Prudential Regulations for SME Financing", "id": "sme-prs",
         "normalized_title": "prudential regulations for sme financing",
         "doc_type": "regulation", "url": SME_PDF_URL, "route": laws.ROUTE_PDF,
         "listed_date": None, "version_label": None, "effective_from": None, "order": 0},
        {"title": "Credit Bureau Act 2015", "id": "cb-act",
         "normalized_title": "credit bureau act 2015", "doc_type": "law",
         "url": "https://www.sbp.org.pk/assets/documents/laws_regulations/CB.pdf",
         "route": laws.ROUTE_PDF, "listed_date": None, "version_label": None,
         "effective_from": None, "order": 1},
    ]
    monkeypatch.setattr(laws, "fetch_listing", lambda force=False: [dict(r) for r in rows])
    monkeypatch.setattr(laws, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        laws, "extract_document_text", lambda path, ft: ("text", "extracted", None)
    )

    def fake_download(document_id, url, force=False):
        destination = tmp_path / document_id / "f.pdf"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"%PDF-1.7\n" + url.encode())
        return destination, f"hash-{url}", None

    monkeypatch.setattr(laws, "download_law_file", fake_download)

    # A law-only pass would normally skip the SME regulation entirely.
    laws.sync_laws(db, doc_types=["law"], delay=0, skip_indexing=True)
    assert db.query(RegDocument).filter(RegDocument.id == "sme-prs").first() is None

    add_document(db, "sme-prs", "Prudential Regulations for SME Financing",
                 source_url=SME_PDF_URL)
    db.query(RegDocument).filter(RegDocument.id == "sme-prs").one().refetch_requested = 1
    db.commit()

    laws.sync_laws(db, doc_types=["law"], delay=0, skip_indexing=True)

    document = db.query(RegDocument).filter(RegDocument.id == "sme-prs").one()
    assert len(document.versions) == 1          # it was fetched despite the filter
    assert document.refetch_requested == 0      # and the request was cleared
