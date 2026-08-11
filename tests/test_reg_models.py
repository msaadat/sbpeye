"""Phase 1 of the laws & regulations plan: the three RegDocument* tables exist,
migrate into an already-populated database, and hold the shapes the later phases
depend on (hierarchy, parallel editions, circular dedupe).

See docs/LAWS_REGULATIONS_PLAN.md.
"""

from datetime import datetime

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sbpeye.database import Base, _ensure_columns
from sbpeye.models import (
    Circular,
    RegDocument,
    RegDocumentLink,
    RegDocumentVersion,
)

REG_TABLES = ("reg_documents", "reg_document_versions", "reg_document_links")


def make_engine():
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def make_session(engine=None):
    engine = engine or make_engine()
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False)()


def make_document(document_id: str = "doc-1", **overrides) -> RegDocument:
    fields = dict(
        id=document_id,
        title="Prudential Regulations for SME Financing",
        normalized_title="prudential regulations for sme financing",
        doc_type="regulation",
        source_url="https://www.sbp.org.pk/assets/documents/laws_regulations/sme-prs.pdf",
        first_seen_at=datetime(2026, 7, 24),
        last_seen_at=datetime(2026, 7, 24),
    )
    fields.update(overrides)
    return RegDocument(**fields)


def make_version(version_id: str, document_id: str, **overrides) -> RegDocumentVersion:
    fields = dict(
        id=version_id,
        document_id=document_id,
        content_hash=f"hash-{version_id}",
        file_url="https://www.sbp.org.pk/assets/documents/laws_regulations/sme-prs.pdf",
        file_type="pdf",
        first_seen_at=datetime(2026, 7, 24),
        last_seen_at=datetime(2026, 7, 24),
    )
    fields.update(overrides)
    return RegDocumentVersion(**fields)


def test_fresh_database_creates_the_reg_tables():
    engine = make_engine()
    Base.metadata.create_all(engine)

    tables = set(inspect(engine).get_table_names())
    assert set(REG_TABLES) <= tables


def test_existing_database_gains_missing_columns_in_place():
    """A database created by an earlier build keeps its rows and gains the new columns."""
    engine = make_engine()
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE reg_documents (id VARCHAR PRIMARY KEY, title VARCHAR)"
        ))
        conn.execute(text(
            "INSERT INTO reg_documents (id, title) VALUES ('doc-1', 'Banking Companies Ordinance')"
        ))

    _ensure_columns(bind=engine)

    columns = {c["name"] for c in inspect(engine).get_columns("reg_documents")}
    for expected in ("normalized_title", "doc_type", "parent_id", "delisted_at", "tags"):
        assert expected in columns

    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT title, delisted_at, is_external FROM reg_documents WHERE id = 'doc-1'"
        )).one()
    assert row.title == "Banking Companies Ordinance"
    assert row.delisted_at is None
    assert row.is_external == 0


def test_ensure_columns_is_idempotent():
    engine = make_engine()
    Base.metadata.create_all(engine)

    _ensure_columns(bind=engine)
    before = {t: {c["name"] for c in inspect(engine).get_columns(t)} for t in REG_TABLES}
    _ensure_columns(bind=engine)
    after = {t: {c["name"] for c in inspect(engine).get_columns(t)} for t in REG_TABLES}

    assert before == after


def test_column_defaults_match_the_archive_semantics():
    """A newly captured version is live, current, unextracted and never delisted."""
    db = make_session()
    db.add(make_document())
    db.add(make_version("ver-1", "doc-1"))
    db.commit()

    document = db.query(RegDocument).one()
    version = db.query(RegDocumentVersion).one()

    assert document.delisted_at is None
    assert document.is_external == 0
    assert version.is_current == 1
    assert version.is_vectorized == 0
    assert version.extraction_status == "pending"
    assert version.source == "live"


def test_document_holds_multiple_versions_with_one_current():
    """Parallel editions coexist; the future-dated one stays pending, not current."""
    db = make_session()
    db.add(make_document())
    db.add(make_version(
        "ver-oct-2024",
        "doc-1",
        version_label="updated till October 07, 2024",
        effective_from=datetime(2024, 10, 7),
        is_current=1,
    ))
    db.add(make_version(
        "ver-jan-2026",
        "doc-1",
        version_label="to be applicable from January 1, 2026",
        effective_from=datetime(2026, 1, 1),
        is_current=0,
    ))
    db.commit()

    document = db.query(RegDocument).one()
    assert len(document.versions) == 2
    assert document.current_version.id == "ver-oct-2024"

    pending = [v for v in document.versions if not v.is_current]
    assert [v.id for v in pending] == ["ver-jan-2026"]


def test_current_version_is_none_for_a_stub_document():
    db = make_session()
    db.add(make_document(
        "doc-external",
        title="Banking Companies Ordinance, 1962 (being updated)",
        is_external=1,
        source_url="https://pakistancode.gov.pk/english/some-ordinance",
    ))
    db.commit()

    assert db.query(RegDocument).one().current_version is None


def test_hierarchy_nests_at_least_three_levels_deep():
    """FE Manual -> Appendix III -> its own children: a child can be a container."""
    db = make_session()
    db.add(make_document("fe-manual", title="Foreign Exchange Manual", doc_type="law",
                         page_slug="foreign-exchange-manual"))
    db.add(make_document("fe-chapter-12", title="Chapter 12", parent_id="fe-manual",
                         part_label="Chapter 12", part_order=12))
    db.add(make_document("fe-appendix-3", title="Appendix III", parent_id="fe-manual",
                         part_label="Appendix III", part_order=23,
                         page_slug="fe-manual-appendix-iii"))
    db.add(make_document("fe-appendix-3-notif-1", title="FE Circular No. 1",
                         parent_id="fe-appendix-3", part_label="1", part_order=1))
    db.commit()

    manual = db.query(RegDocument).filter(RegDocument.id == "fe-manual").one()
    assert [c.id for c in manual.children] == ["fe-chapter-12", "fe-appendix-3"]

    appendix = db.query(RegDocument).filter(RegDocument.id == "fe-appendix-3").one()
    assert appendix.parent.id == "fe-manual"
    assert [c.id for c in appendix.children] == ["fe-appendix-3-notif-1"]


def test_html_content_version_carries_text_instead_of_a_file():
    db = make_session()
    db.add(make_document("fe-appendix-3", title="Appendix III"))
    db.add(make_version(
        "ver-html",
        "fe-appendix-3",
        file_url=None,
        local_path=None,
        file_type="html",
        content_text="FE notifications, inline.",
        extraction_status="success",
    ))
    db.commit()

    version = db.query(RegDocumentVersion).one()
    assert version.file_url is None
    assert version.local_path is None
    assert version.content_text == "FE notifications, inline."


def test_circular_typed_row_points_at_the_circular_instead_of_duplicating_it():
    db = make_session()
    db.add(Circular(
        id="circ-1",
        reference="IBD Circular No. 04 of 2020",
        title="Shariah Governance Framework",
        department="IBD",
        date=datetime(2020, 6, 1),
        url="https://www.sbp.org.pk/circulars/ibd-04-2020",
        content_text="Body",
    ))
    db.add(make_document(
        "doc-guideline",
        title="Shariah Governance Framework",
        doc_type="guideline",
        source_url="https://www.sbp.org.pk/circulars/ibd-04-2020",
        circular_id="circ-1",
    ))
    db.commit()

    document = db.query(RegDocument).one()
    assert document.circular.reference == "IBD Circular No. 04 of 2020"
    assert document.versions == []


def test_links_are_navigable_from_both_the_circular_and_the_document():
    db = make_session()
    db.add(Circular(
        id="circ-1",
        reference="BPRD Circular No. 01 of 2026",
        title="Amendments to Prudential Regulations",
        department="BPRD",
        date=datetime(2026, 1, 15),
        url="https://www.sbp.org.pk/circulars/bprd-01-2026",
        content_text="Body",
    ))
    db.add(make_document())
    db.add(RegDocumentLink(
        circular_id="circ-1",
        document_id="doc-1",
        link_type="amends",
        detected_via="ai",
        confidence=0.9,
    ))
    db.commit()

    circular = db.query(Circular).one()
    assert [link.document.title for link in circular.reg_links] == [
        "Prudential Regulations for SME Financing"
    ]

    document = db.query(RegDocument).one()
    link = document.circular_links[0]
    assert link.circular.reference == "BPRD Circular No. 01 of 2026"
    assert link.detected_via == "ai"
    assert link.created_at is not None


def test_deleting_a_document_takes_its_versions_and_links_with_it():
    """Sync never deletes documents, but a cascade keeps ad-hoc cleanup from orphaning rows."""
    db = make_session()
    db.add(Circular(
        id="circ-1",
        reference="BPRD Circular No. 01 of 2026",
        title="Amendments to Prudential Regulations",
        department="BPRD",
        date=datetime(2026, 1, 15),
        url="https://www.sbp.org.pk/circulars/bprd-01-2026",
        content_text="Body",
    ))
    db.add(make_document())
    db.add(make_version("ver-1", "doc-1"))
    db.add(RegDocumentLink(
        circular_id="circ-1", document_id="doc-1", link_type="references"
    ))
    db.commit()

    db.delete(db.query(RegDocument).one())
    db.commit()

    assert db.query(RegDocumentVersion).count() == 0
    assert db.query(RegDocumentLink).count() == 0
