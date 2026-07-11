"""Annexure-based withdrawal/supersession harvesting.

Some circulars withdraw/supersede others via a list in an attached annexure PDF instead of
naming them in the cover letter (e.g. BPRD Circular No. 04 of 2025 / BC&FRF). These tests pin
the harvesting pipeline: prefix normalization for spaced ampersands, attachment selection by
label, deterministic reference harvesting, dedupe, and the safety skips.
"""

import json
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sbpeye.ai import AIClient, AIConfig, RELATIONSHIP_CONTEXT_CHARS
from sbpeye.database import Base
from sbpeye.link_routing import normalize_reference
from sbpeye.models import Attachment, Circular, CircularRelationship
from sbpeye.supersession import ANNEXURE_LIST_CONFIDENCE, apply_annexure_supersession

# Excerpt of the real Annexure-A list from BPRD Circular No. 04 of 2025.
ANNEXURE_TEXT = """
List of Circulars / Circular Letters stand Withdrawn/Superseded
1. BPRD Circular No. 12 of 2011
2. BC&CPD Circular No. 08 of 2021
3. IH&SMEFD Circular Letter No. 05 of 2010
4. PSD Circular No. 1 of 2015
"""


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def make_circular(circular_id, reference, title="Some circular", year=2020):
    return Circular(
        id=circular_id,
        title=title,
        department="BPRD",
        reference=reference,
        date=datetime(year, 1, 1),
        url=f"https://www.sbp.org.pk/{circular_id}.htm",
        content_text="Body",
    )


def annexure_rels(label="Annexure-A", action="supersedes", flagged=True):
    return {
        "amends": [], "supersedes": [], "cancels": [], "adds_to": [], "clarifies": [],
        "supersedes_all_previous": False,
        "subject": "",
        "references_attachment_list": flagged,
        "attachment_list_label": label,
        "attachment_list_action": action,
    }


def seed(db, annexure_filename="C4-Annexure-A_1.pdf", annexure_text=ANNEXURE_TEXT):
    source = make_circular("source", "BPRD Circular No. 04 of 2025", year=2025)
    targets = [
        make_circular("t-bprd", "BPRD Circular No. 12 of 2011", year=2011),
        # Stored with spaces around '&' as on the SBP site; annexure writes 'BC&CPD'.
        make_circular("t-bccpd", "BC & CPD Circular No. 08 of 2021", year=2021),
        make_circular("t-ihsmefd", "IH & SMEFD Circular Letter No. 05 of 2010", year=2010),
        # 'PSD Circular No. 1 of 2015' has no counterpart -> unresolved row expected.
    ]
    attachments = [
        Attachment(
            id="annex-a", circular_id=source.id, filename=annexure_filename,
            original_url="https://www.sbp.org.pk/files/annex-a.pdf", file_type="pdf",
            extraction_status="extracted", content_text=annexure_text,
        ),
        Attachment(
            id="framework", circular_id=source.id, filename="C4-BC_FRF_3.pdf",
            original_url="https://www.sbp.org.pk/files/framework.pdf", file_type="pdf",
            extraction_status="extracted",
            # A citation inside the main framework that must NOT be harvested.
            content_text="As per ACD Circular No. 99 of 2009, banks shall comply.",
        ),
    ]
    db.add_all([source, *targets, *attachments])
    db.commit()
    return source


def test_spaced_ampersand_prefixes_normalize_identically():
    assert (
        normalize_reference("BC & CPD Circular No. 08 of 2021")
        == normalize_reference("BC&CPD Circular No.8 of 2021")
        == "BC&CPD CIRCULAR NO 8 OF 2021"
    )
    assert normalize_reference("BPRD Circular No. 04 of 2025") == "BPRD CIRCULAR NO 4 OF 2025"


def test_harvests_labelled_annexure_and_resolves_spaced_prefixes():
    db = make_session()
    source = seed(db)

    added = apply_annexure_supersession(db, source, annexure_rels(), warn=lambda *_: None)
    db.commit()

    rows = db.query(CircularRelationship).filter_by(source_id="source").all()
    assert len(added) == len(rows) == 4
    assert all(row.type == "supersedes" for row in rows)
    assert all(row.confidence == ANNEXURE_LIST_CONFIDENCE for row in rows)
    by_ref = {row.target_reference: row.target_id for row in rows}
    assert by_ref["BPRD Circular No. 12 of 2011"] == "t-bprd"
    assert by_ref["BC&CPD Circular No. 8 of 2021"] == "t-bccpd"
    assert by_ref["IH&SMEFD Circular Letter No. 5 of 2010"] == "t-ihsmefd"
    assert by_ref["PSD Circular No. 1 of 2015"] is None
    # The framework PDF's in-text citation must not leak in.
    assert not any("ACD" in ref for ref in by_ref)


def test_cancels_action_and_flag_off():
    db = make_session()
    source = seed(db)

    assert apply_annexure_supersession(db, source, annexure_rels(flagged=False)) == []

    added = apply_annexure_supersession(
        db, source, annexure_rels(action="cancels"), warn=lambda *_: None
    )
    assert {rel.type for rel in added} == {"cancels"}


def test_dedupes_against_existing_relationships():
    db = make_session()
    source = seed(db)
    db.add(CircularRelationship(
        source_id="source", target_id="t-bprd",
        target_reference="BPRD Circular No. 12 of 2011", type="supersedes",
    ))
    # Same circular, different spelling: must be caught by normalized-reference dedupe.
    db.add(CircularRelationship(
        source_id="source", target_id=None,
        target_reference="BC & CPD Circular No. 08 of 2021", type="amends",
    ))
    db.commit()

    added = apply_annexure_supersession(db, source, annexure_rels(), warn=lambda *_: None)
    assert {rel.target_reference for rel in added} == {
        "IH&SMEFD Circular Letter No. 5 of 2010",
        "PSD Circular No. 1 of 2015",
    }


def test_skips_when_no_annexure_attachment_matches():
    db = make_session()
    source = seed(db, annexure_filename="C4-Something-Else.pdf")

    warnings = []
    added = apply_annexure_supersession(db, source, annexure_rels(), warn=warnings.append)
    assert added == []
    assert db.query(CircularRelationship).count() == 0
    assert any("manual review" in message for message in warnings)


def test_falls_back_to_annex_filename_when_label_differs():
    db = make_session()
    source = seed(db, annexure_filename="C4-Annexure_1.pdf")

    added = apply_annexure_supersession(
        db, source, annexure_rels(label="Annexure-A"), warn=lambda *_: None
    )
    assert len(added) == 4


def test_skips_implausibly_large_harvest():
    big_list = "\n".join(f"BPRD Circular No. {n % 300} of {1990 + n % 30}" for n in range(400))
    db = make_session()
    source = seed(db, annexure_text=big_list)

    warnings = []
    added = apply_annexure_supersession(db, source, annexure_rels(), warn=warnings.append)
    assert added == []
    assert any("above the limit" in message for message in warnings)


def test_skips_self_reference_in_annexure():
    db = make_session()
    source = seed(db, annexure_text="BPRD Circular No. 04 of 2025\nBPRD Circular No. 12 of 2011")

    added = apply_annexure_supersession(db, source, annexure_rels(), warn=lambda *_: None)
    assert [rel.target_reference for rel in added] == ["BPRD Circular No. 12 of 2011"]


def test_extract_relationships_parses_attachment_list_fields(monkeypatch):
    client = AIClient(AIConfig())
    captured = {}

    def fake_complete(system, user, **kwargs):
        captured["user"] = user
        return json.dumps({
            "amends": [], "supersedes": [], "cancels": [], "adds_to": [], "clarifies": [],
            "supersedes_all_previous": False, "subject": "",
            "references_attachment_list": True,
            "attachment_list_label": " Annexure-A ",
            "attachment_list_action": "supersedes",
        })

    monkeypatch.setattr(client, "_complete", fake_complete)
    long_tail = "x" * 10_000 + " the circulars listed at Annexure-A stand withdrawn/superseded"
    rels = client.extract_relationships("Title", "BPRD Circular No. 04 of 2025", long_tail)

    assert rels["references_attachment_list"] is True
    assert rels["attachment_list_label"] == "Annexure-A"
    assert rels["attachment_list_action"] == "supersedes"
    # The cover letter is no longer clipped at max_context_tokens (4000 chars).
    assert "stand withdrawn/superseded" in captured["user"]
    assert len(long_tail) < RELATIONSHIP_CONTEXT_CHARS


def test_extract_relationships_defaults_when_fields_missing(monkeypatch):
    client = AIClient(AIConfig())
    monkeypatch.setattr(client, "_complete", lambda *a, **k: json.dumps({
        "amends": [], "supersedes": [], "cancels": [], "adds_to": [], "clarifies": [],
        "supersedes_all_previous": False, "subject": "",
    }))
    rels = client.extract_relationships("Title", "Ref", "Body")

    assert rels["references_attachment_list"] is False
    assert rels["attachment_list_label"] == ""
    assert rels["attachment_list_action"] == "supersedes"
