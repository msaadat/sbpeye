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
from sbpeye.main import _truncate_chat_messages, get_chat_session
from sbpeye.models import Attachment, ChatMessage, ChatSession, Circular
from sbpeye.search import SearchEngine


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


def test_search_department_filter_accepts_partial_department(monkeypatch):
    import sbpeye.search as search_module

    monkeypatch.setattr(search_module, "collection", FailingCollection())
    monkeypatch.setattr(search_module, "embedding_backend", FailingEmbeddings())
    db = make_session()
    db.add_all([
        Circular(
            id="dmmd",
            reference="DMMD Circular No. 04",
            title="Maintenance of Statutory Cash Reserve Requirement",
            department="Domestic Markets & Monetary Management (DMMD)",
            date=datetime(2018, 3, 8),
            content_text="Cash reserve requirement applies.",
        ),
        Circular(
            id="bprd",
            reference="BPRD Circular No. 01",
            title="Other requirement",
            department="Banking Policy & Regulations (BPRD)",
            date=datetime(2018, 3, 8),
            content_text="Cash reserve requirement applies.",
        ),
    ])
    db.commit()

    results, _ = SearchEngine().search(
        "cash reserve requirement",
        db,
        department="Domestic Markets & Monetary Management",
    )

    assert [item["circular"].id for item in results] == ["dmmd"]


def test_reference_search_understands_dated_year():
    db = make_session()
    db.add_all([
        Circular(
            id="old",
            reference="DMMD Circular No. 04",
            title="Maintenance of Statutory Cash Reserve Requirement",
            department="Domestic Markets & Monetary Management (DMMD)",
            date=datetime(2018, 3, 8),
            content_text="Old CRR circular.",
        ),
        Circular(
            id="new",
            reference="DMMD Circular No. 04",
            title="Policy Rate",
            department="Domestic Markets & Monetary Management (DMMD)",
            date=datetime(2025, 5, 5),
            content_text="New policy circular.",
        ),
        Circular(
            id="other-number",
            reference="DMMD Circular No. 24",
            title="Special Cash Reserve Account",
            department="Domestic Markets & Monetary Management (DMMD)",
            date=datetime(2018, 11, 30),
            content_text="Different circular.",
        ),
    ])
    db.commit()

    results = SearchEngine._search_by_reference(
        "DMMD Circular No. 04 dated March 08, 2018",
        db,
        limit=5,
    )

    assert [item.id for item in results] == ["old"]


def test_circular_details_reports_ambiguous_reference():
    db = make_session()
    db.add_all([
        Circular(
            id="old",
            reference="DMMD Circular No. 04",
            title="Old circular",
            department="Domestic Markets & Monetary Management (DMMD)",
            date=datetime(2018, 3, 8),
            content_text="Old CRR circular.",
        ),
        Circular(
            id="new",
            reference="DMMD Circular No. 04",
            title="New circular",
            department="Domestic Markets & Monetary Management (DMMD)",
            date=datetime(2025, 5, 5),
            content_text="New policy circular.",
        ),
    ])
    db.commit()
    client = AIClient(AIConfig())

    payload = json.loads(client._execute_tool(
        "get_circular_details",
        {"circular_reference": "DMMD Circular No. 04"},
        db,
    ))

    assert payload["error"].startswith("Ambiguous circular reference")
    assert [item["date"] for item in payload["candidates"]] == [
        "2025-05-05",
        "2018-03-08",
    ]


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


def test_chat_session_returns_each_messages_context_snapshot():
    db = make_session()
    add_circular(db, "one", "One")
    session = ChatSession(id="session", title="Test", circular_ids="[]")
    message = ChatMessage(
        id="message",
        session_id=session.id,
        role="user",
        content="Question",
        circular_ids=json.dumps(["one"]),
    )
    db.add_all([session, message])
    db.commit()

    payload = asyncio.run(get_chat_session(session.id, db))

    assert payload["messages"][0]["circular_ids"] == ["one"]


def test_truncate_chat_messages_can_preserve_and_edit_target_turn():
    db = make_session()
    session = ChatSession(id="session", title="Test", circular_ids="[]")
    messages = [
        ChatMessage(id="01", session_id=session.id, role="user", content="First"),
        ChatMessage(id="02", session_id=session.id, role="assistant", content="Answer"),
        ChatMessage(id="03", session_id=session.id, role="user", content="Second"),
        ChatMessage(id="04", session_id=session.id, role="assistant", content="Answer 2"),
    ]
    db.add_all([session, *messages])
    db.commit()

    target = _truncate_chat_messages(
        db, session.id, "03", include_message=False
    )
    target.content = "Edited second"
    db.commit()

    remaining = db.query(ChatMessage).order_by(ChatMessage.id).all()
    assert [(message.id, message.content) for message in remaining] == [
        ("01", "First"),
        ("02", "Answer"),
        ("03", "Edited second"),
    ]


def test_truncate_chat_messages_can_delete_target_and_following_history():
    db = make_session()
    session = ChatSession(id="session", title="Test", circular_ids="[]")
    messages = [
        ChatMessage(id="01", session_id=session.id, role="user", content="First"),
        ChatMessage(id="02", session_id=session.id, role="assistant", content="Answer"),
        ChatMessage(id="03", session_id=session.id, role="user", content="Second"),
    ]
    db.add_all([session, *messages])
    db.commit()

    _truncate_chat_messages(db, session.id, "02", include_message=True)
    db.commit()

    assert [message.id for message in db.query(ChatMessage).all()] == ["01"]
