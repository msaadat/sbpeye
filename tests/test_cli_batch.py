"""Characterization tests for the CLI AI-batch functions.

These pin the per-task behavior (which DB field + ``*_generated_at`` timestamp is set,
how ``--refresh`` and ``--limit`` select circulars, child-row creation for relationships)
before the Phase 1a dedup collapses them into a shared driver. They call the ``_run_*``
functions directly with a fake AI client and an in-memory DB.
"""

import json
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sbpeye.database import Base
from sbpeye.models import Circular, CircularRelationship
from sbpeye.cli import commands


class FakeClient:
    def summarize(self, title, content_text):
        return f"summary of {title}"

    def generate_tags(self, title, content_text):
        return ["AML", "KYC"]

    def generate_checklist(self, circular, delay=0.0, **kwargs):
        return {
            "schema_version": 2,
            "status": "ok",
            "checklist_items": [{"classification": "required"}],
        }

    def extract_relationships(self, title, reference, content_text):
        return {"amends": [], "supersedes": [], "cancels": [], "adds_to": [], "clarifies": []}


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def add_circular(db, circular_id, **overrides):
    fields = dict(
        id=circular_id,
        reference=f"Ref {circular_id}",
        title=f"Circular {circular_id}",
        department="BPRD",
        date=datetime(2025, 1, 1),
        url=f"https://www.sbp.org.pk/{circular_id}.htm",
        content_text="Body text",
    )
    fields.update(overrides)
    circular = Circular(**fields)
    db.add(circular)
    db.commit()
    return circular


def test_summarize_sets_summary_and_timestamp():
    db = make_session()
    add_circular(db, "c1")
    commands._run_summarize(db, FakeClient(), None, (), (), 0, False, False, 0)
    c = db.query(Circular).filter(Circular.id == "c1").first()
    assert c.summary == "summary of Circular c1"
    assert c.summary_generated_at is not None


def test_summarize_skips_already_summarized_unless_refresh():
    db = make_session()
    add_circular(db, "done", summary="existing", summary_generated_at=datetime(2025, 1, 1))
    add_circular(db, "todo")

    commands._run_summarize(db, FakeClient(), None, (), (), 0, False, False, 0)
    assert db.query(Circular).filter(Circular.id == "done").first().summary == "existing"
    assert db.query(Circular).filter(Circular.id == "todo").first().summary == "summary of Circular todo"

    # With refresh, the already-summarized circular is overwritten too.
    commands._run_summarize(db, FakeClient(), None, (), (), 0, True, False, 0)
    assert db.query(Circular).filter(Circular.id == "done").first().summary == "summary of Circular done"


def test_summarize_skips_circular_without_content():
    db = make_session()
    add_circular(db, "empty", content_text=None)
    commands._run_summarize(db, FakeClient(), None, (), (), 0, False, False, 0)
    assert db.query(Circular).filter(Circular.id == "empty").first().summary is None


def test_summarize_honors_limit():
    db = make_session()
    add_circular(db, "a", date=datetime(2025, 3, 1))
    add_circular(db, "b", date=datetime(2025, 2, 1))
    add_circular(db, "c", date=datetime(2025, 1, 1))
    commands._run_summarize(db, FakeClient(), None, (), (), 1, False, False, 0)
    summarized = [c for c in db.query(Circular).all() if c.summary]
    assert len(summarized) == 1
    # Ordered by date desc, so the newest ("a") is processed first.
    assert summarized[0].id == "a"


def test_tags_sets_json_tags_and_timestamp():
    db = make_session()
    add_circular(db, "c1")
    commands._run_tags(db, FakeClient(), None, (), (), 0, False, False, 0)
    c = db.query(Circular).filter(Circular.id == "c1").first()
    assert json.loads(c.tags) == ["AML", "KYC"]
    assert c.tags_generated_at is not None


def test_checklist_sets_checklist_and_timestamp():
    db = make_session()
    add_circular(db, "c1")
    commands._run_checklist(db, FakeClient(), None, (), (), 0, False, False, 0)
    c = db.query(Circular).filter(Circular.id == "c1").first()
    stored = json.loads(c.compliance_checklist)
    assert stored["schema_version"] == 2
    assert c.checklist_generated_at is not None


def test_relationships_sets_timestamp_and_creates_rows():
    db = make_session()
    add_circular(db, "src")
    add_circular(db, "tgt", reference="Ref tgt")

    class RelClient(FakeClient):
        def extract_relationships(self, title, reference, content_text):
            return {"supersedes": ["Ref tgt"], "amends": [], "cancels": [], "adds_to": [], "clarifies": []}

    commands._run_relationships(db, RelClient(), None, (), (), 0, False, False, 0)
    src = db.query(Circular).filter(Circular.id == "src").first()
    assert src.relationships_generated_at is not None
    rels = db.query(CircularRelationship).filter(CircularRelationship.source_id == "src").all()
    assert len(rels) == 1
    assert rels[0].type == "supersedes"
    assert rels[0].target_reference == "Ref tgt"


def test_relationships_refresh_clears_existing():
    db = make_session()
    add_circular(db, "src")
    db.add(CircularRelationship(source_id="src", target_id=None, type="amends", target_reference="old"))
    db.commit()

    # Without refresh, a circular that already has relationships is skipped entirely.
    commands._run_relationships(db, FakeClient(), None, (), (), 0, False, False, 0)
    assert db.query(CircularRelationship).filter(CircularRelationship.source_id == "src").count() == 1

    # With refresh, existing relationships are deleted and re-extracted (none this time).
    commands._run_relationships(db, FakeClient(), None, (), (), 0, True, False, 0)
    assert db.query(CircularRelationship).filter(CircularRelationship.source_id == "src").count() == 0
