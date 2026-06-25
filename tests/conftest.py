"""Shared fixtures for route- and CLI-level characterization tests.

These tests exercise the FastAPI app and CLI batch functions against an isolated
in-memory SQLite database, with the AI client stubbed so no network calls happen.
The goal is to pin current behavior before refactoring.
"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sbpeye.database import Base, get_db
from sbpeye.models import Circular
import sbpeye.main as main_module


class FakeAIConfig:
    max_context_tokens = 4000


class FakeAIClient:
    """Deterministic stand-in for AIClient used by chat route tests."""

    def __init__(self):
        self.config = FakeAIConfig()

    def chat(self, messages, db, **kwargs):
        return "fake assistant reply"

    def stream_chat(self, messages, db, **kwargs):
        yield "fake "
        yield "stream reply"


@pytest.fixture
def db_factory():
    """An isolated in-memory DB shared across every connection in one test."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture
def client(db_factory, monkeypatch):
    """A TestClient wired to the in-memory DB with the AI layer stubbed out."""
    monkeypatch.setattr(main_module, "SessionLocal", db_factory)
    monkeypatch.setattr(main_module, "get_ai_client", lambda db=None: FakeAIClient())
    monkeypatch.setattr(main_module, "_build_chat_circulars_context", lambda *a, **k: "")

    def override_get_db():
        db = db_factory()
        try:
            yield db
        finally:
            db.close()

    main_module.app.dependency_overrides[get_db] = override_get_db
    with TestClient(main_module.app) as test_client:
        yield test_client, db_factory
    main_module.app.dependency_overrides.clear()


def make_circular(circular_id: str = "circular-1", **overrides) -> Circular:
    fields = dict(
        id=circular_id,
        reference="BPRD Circular No. 01 of 2025",
        title="Test circular",
        department="BPRD",
        date=datetime(2025, 1, 1),
        url=f"https://www.sbp.org.pk/{circular_id}.htm",
        content_text="Circular body text",
    )
    fields.update(overrides)
    return Circular(**fields)
