"""Characterization tests for the CLI AI-batch functions.

These pin the per-task behavior (which DB field + ``*_generated_at`` timestamp is set,
how ``--refresh`` and ``--limit`` select circulars, child-row creation for relationships)
before the Phase 1a dedup collapses them into a shared driver. They call the ``_run_*``
functions directly with a fake AI client and an in-memory DB.
"""

import json
from datetime import datetime

import click
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sbpeye.ai import is_rate_limit_error
from sbpeye.database import Base
from sbpeye.models import Circular, CircularRelationship
from sbpeye.cli import commands


class _RateLimit429(Exception):
    """Stand-in for a provider SDK 429 (rate exceeded) error."""

    def __init__(self, message="429 Too Many Requests"):
        super().__init__(message)
        self.status_code = 429


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


def test_is_rate_limit_error_detects_429():
    assert is_rate_limit_error(_RateLimit429()) is True
    assert is_rate_limit_error(Exception("rate_limit_exceeded for model")) is True
    assert is_rate_limit_error(ValueError("invalid json")) is False


def test_summarize_stops_on_first_rate_limit():
    db = make_session()
    add_circular(db, "a", date=datetime(2025, 3, 1))
    add_circular(db, "b", date=datetime(2025, 2, 1))

    class RateLimitedClient(FakeClient):
        def __init__(self):
            self.calls = 0

        def summarize(self, title, content_text):
            self.calls += 1
            raise _RateLimit429()

    client = RateLimitedClient()
    with pytest.raises(click.ClickException):
        commands._run_summarize(db, client, None, (), (), 0, False, False, 0)

    # The batch aborts on the first 429 rather than attempting the rest.
    assert client.calls == 1
    assert db.query(Circular).filter(Circular.id == "b").first().summary is None


def test_batch_continues_past_non_rate_limit_errors():
    db = make_session()
    add_circular(db, "a", date=datetime(2025, 3, 1))
    add_circular(db, "b", date=datetime(2025, 2, 1))

    class FlakyClient(FakeClient):
        def summarize(self, title, content_text):
            if title.endswith("a"):
                raise ValueError("transient parse error")
            return f"summary of {title}"

    commands._run_summarize(db, FlakyClient(), None, (), (), 0, False, False, 0)
    # The non-429 error on "a" is logged and skipped; "b" still gets summarized.
    assert db.query(Circular).filter(Circular.id == "b").first().summary == "summary of Circular b"


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
