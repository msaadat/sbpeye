import asyncio
import json
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sbpeye.ai import AIClient, AIConfig
from sbpeye.chat_retrieval import (
    ScopedChatRetriever,
    build_chat_context,
    estimate_tokens,
)
from sbpeye.database import Base
from sbpeye.main import get_chat_session
from sbpeye.models import Attachment, ChatMessage, ChatSession, Circular


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def add_circular(db, circular_id: str, body: str, attachment_text: str = ""):
    circular = Circular(
        id=circular_id,
        reference=f"REF-{circular_id}",
        title=f"Circular {circular_id}",
        department="BPRD",
        date=datetime(2025, 1, 1),
        url=f"https://www.sbp.org.pk/{circular_id}.htm",
        content_text=body,
    )
    circular.attachments = [
        Attachment(
            id=f"attachment-{circular_id}",
            circular_id=circular_id,
            filename=f"rules-{circular_id}.pdf",
            original_url=f"https://www.sbp.org.pk/rules-{circular_id}.pdf",
            file_type="pdf",
            content_text=attachment_text or None,
            extraction_status="extracted" if attachment_text else "scanned",
            is_vectorized=bool(attachment_text),
        )
    ]
    db.add(circular)
    db.commit()
    return circular


class FailingCollection:
    def query(self, **kwargs):
        raise RuntimeError("vector store unavailable")


class FailingEmbeddings:
    def embed_queries(self, texts):
        raise RuntimeError("embedding service unavailable")


def disable_vectors(monkeypatch):
    import sbpeye.chat_retrieval as retrieval_module

    monkeypatch.setattr(retrieval_module, "collection", FailingCollection())
    monkeypatch.setattr(retrieval_module, "embedding_backend", FailingEmbeddings())


def test_manifest_exposes_attachment_details_without_unavailable_text(monkeypatch):
    disable_vectors(monkeypatch)
    db = make_session()
    add_circular(db, "one", "Circular body")

    manifest = ScopedChatRetriever(db, ["one"]).attachment_manifest()

    assert "[[attachment:attachment-one|rules-one.pdf]]" in manifest
    assert "type=pdf" in manifest
    assert "extraction_status=scanned" in manifest
    assert "text_available=no" in manifest
    assert "indexed=no" in manifest
    assert "https://www.sbp.org.pk/rules-one.pdf" in manifest


def test_lexical_retrieval_is_scoped_and_works_without_vectors(monkeypatch):
    disable_vectors(monkeypatch)
    db = make_session()
    add_circular(db, "selected", "General requirements", "Submit quuxreport quarterly.")
    add_circular(db, "other", "General requirements", "Confidential othertopic details.")
    retriever = ScopedChatRetriever(db, ["selected"])

    results = retriever.search("quuxreport", token_budget=500)
    forbidden = retriever.search("othertopic", token_budget=500)

    assert len(results) == 1
    assert results[0]["source_type"] == "attachment"
    assert results[0]["citation"] == (
        "[[attachment:attachment-selected|rules-selected.pdf]]"
    )
    assert "quuxreport" in results[0]["passage"]
    assert forbidden == []


def test_vector_query_uses_selected_circular_filter(monkeypatch):
    db = make_session()
    add_circular(db, "selected", "A " * 2000, "Selected semantic passage " * 300)
    add_circular(db, "other", "Other body", "Other semantic passage")
    calls = []

    class FakeCollection:
        def query(self, **kwargs):
            calls.append(kwargs)
            return {
                "ids": [[
                    "attachment-selected__chunk_0",
                    "attachment-other__chunk_0",
                ]],
                "metadatas": [[{}, {}]],
            }

    class FakeEmbeddings:
        def embed_queries(self, texts):
            return [[0.1]]

    import sbpeye.chat_retrieval as retrieval_module

    monkeypatch.setattr(retrieval_module, "collection", FakeCollection())
    monkeypatch.setattr(retrieval_module, "embedding_backend", FakeEmbeddings())
    results = ScopedChatRetriever(db, ["selected"]).search(
        "semantic", token_budget=500
    )

    assert calls[0]["where"] == {"circular_id": "selected"}
    assert results
    assert all("other" not in result["citation"] for result in results)


def test_context_includes_small_text_and_bounds_retrieved_passages(monkeypatch):
    disable_vectors(monkeypatch)
    db = make_session()
    circular = add_circular(
        db,
        "one",
        "Short circular body.",
        ("background " * 500) + "needle requirement " + ("tail " * 500),
    )

    context, retriever = build_chat_context(db, [circular.id], "needle", 800)
    results = retriever.search("needle", token_budget=200)

    assert "Short circular body." in context
    assert "Automatically retrieved passages" in context
    assert "needle requirement" in context
    assert sum(estimate_tokens(item["passage"]) for item in results) <= 200


def test_selected_document_tool_cannot_accept_a_different_scope(monkeypatch):
    disable_vectors(monkeypatch)
    db = make_session()
    add_circular(db, "selected", "Selected regulation", "selectedterm applies")
    add_circular(db, "other", "Other regulation", "forbiddenterm applies")
    client = AIClient(AIConfig())

    payload = json.loads(client._execute_tool(
        "search_selected_documents",
        {"query": "forbiddenterm", "circular_ids": ["other"]},
        db,
        ["selected"],
    ))

    assert payload == {"results": [], "count": 0}


def test_session_context_is_authoritative_including_empty_selection():
    db = make_session()
    add_circular(db, "one", "One")
    add_circular(db, "two", "Two")
    session = ChatSession(
        id="session",
        title="Test",
        circular_ids=json.dumps(["one"]),
    )
    message = ChatMessage(
        id="message",
        session_id=session.id,
        role="user",
        content="Question",
        circular_ids=json.dumps(["one", "two"]),
    )
    db.add_all([session, message])
    db.commit()

    payload = asyncio.run(get_chat_session(session.id, db))
    assert [item["id"] for item in payload["circulars"]] == ["one"]

    session.circular_ids = "[]"
    db.commit()
    payload = asyncio.run(get_chat_session(session.id, db))
    assert payload["circulars"] == []
