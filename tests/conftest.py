"""Shared fixtures for route- and CLI-level characterization tests.

These tests exercise the FastAPI app and CLI batch functions against an isolated
in-memory SQLite database, with the AI client stubbed so no network calls happen.
The goal is to pin current behavior before refactoring.

Every test also runs against an in-memory vector store — see `isolated_vector_store`.
"""

import math
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sbpeye.database import Base, get_db
from sbpeye.models import Circular
import sbpeye.main as main_module


# Modules that bind `collection` at import time from `sbpeye.database`. Each one is a
# separate name that has to be redirected; patching only `sbpeye.database.collection`
# leaves the others pointing at the real store.
_COLLECTION_BINDINGS = (
    "sbpeye.database",
    "sbpeye.search",
    "sbpeye.chat_retrieval",
    "sbpeye.scraper.circulars",
)


class FakeChromaCollection:
    """In-memory stand-in for a Chroma collection.

    Supports the surface the application actually uses — get/add/delete/count/query —
    including `where` metadata filters and a brute-force cosine `query`, so ranking
    assertions still mean something.
    """

    def __init__(self):
        self.records: dict[str, dict] = {}

    # -- writes ----------------------------------------------------------------
    def add(self, documents=None, embeddings=None, metadatas=None, ids=None):
        for index, id_ in enumerate(ids or []):
            self.records[id_] = {
                "document": (documents or [""] * len(ids))[index],
                "embedding": (embeddings or [[0.0]] * len(ids))[index],
                "metadata": (metadatas or [{}] * len(ids))[index] or {},
            }

    def delete(self, ids=None, where=None):
        for id_ in list(ids or []):
            self.records.pop(id_, None)
        if where:
            for id_, record in list(self.records.items()):
                if self._matches(record["metadata"], where):
                    self.records.pop(id_, None)

    # -- reads -----------------------------------------------------------------
    @staticmethod
    def _matches(metadata: dict, where: dict) -> bool:
        return all(metadata.get(key) == value for key, value in (where or {}).items())

    def get(self, ids=None, where=None, limit=None, offset=None, include=None):
        items = [
            (id_, record) for id_, record in self.records.items()
            if (ids is None or id_ in ids) and self._matches(record["metadata"], where)
        ]
        if offset:
            items = items[offset:]
        if limit is not None:
            items = items[:limit]
        include = include if include is not None else ["metadatas", "documents"]
        page = {"ids": [id_ for id_, _ in items]}
        if "metadatas" in include:
            page["metadatas"] = [r["metadata"] for _, r in items]
        if "documents" in include:
            page["documents"] = [r["document"] for _, r in items]
        if "embeddings" in include:
            page["embeddings"] = [r["embedding"] for _, r in items]
        return page

    def query(self, query_embeddings=None, n_results=10, where=None, **kwargs):
        vector = (query_embeddings or [[]])[0]
        scored = []
        for id_, record in self.records.items():
            if not self._matches(record["metadata"], where):
                continue
            scored.append((self._cosine(vector, record["embedding"]), id_, record))
        scored.sort(key=lambda item: -item[0])
        top = scored[:n_results]
        return {
            "ids": [[id_ for _, id_, _ in top]],
            "distances": [[1.0 - score for score, _, _ in top]],
            "metadatas": [[r["metadata"] for _, _, r in top]],
            "documents": [[r["document"] for _, _, r in top]],
        }

    @staticmethod
    def _cosine(left, right) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        dot = sum(a * b for a, b in zip(left, right))
        norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
        return dot / norm if norm else 0.0

    def count(self):
        return len(self.records)


@pytest.fixture(autouse=True)
def isolated_vector_store(monkeypatch):
    """Redirect every module's Chroma handle at a fresh in-memory collection.

    Without this the suite writes to the developer's real `chroma_db/`: `sync_laws`
    reaches `scraper.circulars.collection`, and a full run on 2026-08-14 deleted the
    chunks of 31 real law versions and left 31 fixture chunks behind in the live store.

    Autouse and blanket by design. Isolation that each test has to remember to request is
    isolation that the next test forgets, and the failure is silent — the suite passes
    while quietly corrupting production data. Tests needing their own double simply patch
    over this one.
    """
    import importlib

    fake = FakeChromaCollection()
    for module_name in _COLLECTION_BINDINGS:
        # Import rather than read `sys.modules`: a module the test imports *later* would
        # otherwise bind the real collection and slip past this guard entirely.
        module = importlib.import_module(module_name)
        if hasattr(module, "collection"):
            monkeypatch.setattr(module, "collection", fake)
    return fake


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
