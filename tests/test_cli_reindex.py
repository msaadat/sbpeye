"""Regression tests for ``sbpeye reindex``.

The command rebuilds the shared ``circulars`` Chroma collection. Laws live in that same
collection, so a rebuild that only walks ``Circular`` silently destroys the law arm —
and because ``RegDocumentVersion.is_vectorized`` stays 1, ``index_pending_laws`` then
skips those documents forever, leaving laws lexically findable but semantically
invisible. These tests pin that laws survive a full reindex.

Chroma and the embedding backend are stubbed, so a run is offline and deterministic.
"""

from datetime import datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sbpeye.database import Base
from sbpeye.models import Attachment, Circular, RegDocument, RegDocumentVersion
from sbpeye.cli import commands


class FakeCollection:
    """Minimal in-memory stand-in for a Chroma collection."""

    def __init__(self):
        self.records: dict[str, dict] = {}
        self.dropped = False

    def add(self, documents, embeddings, ids, metadatas):
        for doc, emb, id_, meta in zip(documents, embeddings, ids, metadatas):
            self.records[id_] = {"document": doc, "embedding": emb, "metadata": meta}

    def get(self, ids=None, where=None, limit=None, offset=None, include=None):
        items = list(self.records.items())
        if where:
            (key, value), = where.items()
            items = [(i, r) for i, r in items if r["metadata"].get(key) == value]
        if offset:
            items = items[offset:]
        if limit is not None:
            items = items[:limit]
        return {
            "ids": [i for i, _ in items],
            "metadatas": [r["metadata"] for _, r in items],
            "documents": [r["document"] for _, r in items],
        }

    def delete(self, ids):
        for id_ in ids:
            self.records.pop(id_, None)

    def count(self):
        return len(self.records)

    def law_document_ids(self):
        return {
            r["metadata"]["document_id"]
            for r in self.records.values()
            if r["metadata"].get("kind") == "law"
        }

    def circular_ids(self):
        return {
            r["metadata"]["circular_id"]
            for r in self.records.values()
            if r["metadata"].get("circular_id")
        }


class FakeEmbeddingBackend:
    def embed_documents(self, documents):
        return [[float(len(d) % 7), 1.0, 0.0] for d in documents]

    def embed_queries(self, queries):
        return [[1.0, 0.0, 0.0] for _ in queries]


def make_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False)()


def seed(db):
    """One circular with an attachment, one searchable law, one non-text law."""
    circular = Circular(
        id="circ-1",
        title="Call Center Management",
        reference="BC & CPD Circular No. 03 of 2021",
        department="BC&CPD",
        url="https://example.test/c1",
        content_text="Banks shall operate a call centre for consumer grievances.",
        date=datetime(2021, 5, 1),
    )
    db.add(circular)
    db.add(Attachment(
        id="att-1",
        circular_id="circ-1",
        filename="annexure.pdf",
        original_url="https://example.test/a1.pdf",
        file_type="pdf",
        content_text="Annexure A: minimum staffing for the call centre.",
        is_vectorized=0,
    ))

    law = RegDocument(
        id="law-1",
        title="AML/CFT Regulations",
        doc_type="regulation",
        source_url="https://example.test/aml",
    )
    db.add(law)
    db.add(RegDocumentVersion(
        id="law-1-v1",
        document_id="law-1",
        content_hash="hash-1",
        file_type="pdf",
        content_text="Regulation 1. Customer due diligence requirements apply.",
        is_current=1,
        # Already marked indexed — this is what made the loss self-perpetuating.
        is_vectorized=1,
    ))

    manifest = RegDocument(
        id="law-2",
        title="Container Manifest",
        doc_type="law",
        source_url="https://example.test/container",
    )
    db.add(manifest)
    db.add(RegDocumentVersion(
        id="law-2-v1",
        document_id="law-2",
        content_hash="hash-2",
        file_type="manifest",
        content_text="",
        is_current=1,
        is_vectorized=0,
    ))
    db.commit()


@pytest.fixture
def reindex_env(monkeypatch):
    """Wire the CLI, the scraper module, and the DB to stubs."""
    db = make_session()
    seed(db)
    fake = FakeCollection()
    backend = FakeEmbeddingBackend()

    import sbpeye.database as database
    import sbpeye.scraper.circulars as circulars_mod

    # `scraper.circulars` binds both names at import; `reindex_cmd` resolves them from
    # `sbpeye.database` at call time. Patching both is what a live process needs too.
    monkeypatch.setattr(database, "collection", fake, raising=False)
    monkeypatch.setattr(database, "embedding_backend", backend, raising=False)
    monkeypatch.setattr(circulars_mod, "collection", fake, raising=False)
    monkeypatch.setattr(circulars_mod, "embedding_backend", backend, raising=False)
    monkeypatch.setattr(commands, "SessionLocal", lambda: db)

    return db, fake


def run_reindex():
    from click.testing import CliRunner

    result = CliRunner().invoke(commands.reindex_cmd, [])
    assert result.exit_code == 0, result.output
    return result


def test_reindex_keeps_law_vectors(reindex_env):
    """The defect: a rebuild used to leave the law arm empty."""
    db, fake = reindex_env

    run_reindex()

    assert fake.law_document_ids() == {"law-1"}, (
        "searchable law lost from the shared collection by reindex"
    )
    assert "circ-1" in fake.circular_ids()


def test_reindex_reembeds_laws_already_marked_vectorized(reindex_env):
    """`is_vectorized=1` must not cause the law to be skipped during a full rebuild."""
    db, fake = reindex_env

    run_reindex()

    version = db.query(RegDocumentVersion).filter_by(id="law-1-v1").one()
    assert version.is_vectorized == 1
    assert fake.law_document_ids() == {"law-1"}


def test_reindex_excludes_non_text_law(reindex_env):
    """Manifests are bookkeeping, not text — they must not gain chunks."""
    _, fake = reindex_env

    run_reindex()

    assert "law-2" not in fake.law_document_ids()


def test_reindex_rebuilds_both_fts_indexes(reindex_env):
    db, fake = reindex_env

    run_reindex()

    conn = db.connection()
    assert conn.execute(text("SELECT count(*) FROM circulars_fts")).scalar() == 1
    assert conn.execute(
        text("SELECT count(*) FROM laws_fts WHERE document_id = 'law-1'")
    ).scalar() == 1


def test_reindex_clears_stale_chunks_without_dropping_the_handle(reindex_env):
    """Clearing must empty the collection in place, keeping imported handles valid."""
    db, fake = reindex_env
    fake.add(
        documents=["stale"],
        embeddings=[[0.0, 0.0, 0.0]],
        ids=["circ-gone__chunk_0"],
        metadatas=[{"circular_id": "circ-gone", "doc_type": "circular"}],
    )

    import sbpeye.scraper.circulars as circulars_mod

    run_reindex()

    assert "circ-gone__chunk_0" not in fake.records
    assert circulars_mod.collection is fake, "collection handle was replaced, not cleared"


def test_dry_run_writes_nothing(reindex_env):
    from click.testing import CliRunner

    _, fake = reindex_env
    result = CliRunner().invoke(commands.reindex_cmd, ["--dry-run"])

    assert result.exit_code == 0, result.output
    assert fake.count() == 0
    assert "law/regulation documents" in result.output
