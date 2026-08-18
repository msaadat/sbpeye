import asyncio
import json
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sbpeye.ai import AIClient, AIConfig
from sbpeye.chat_retrieval import (
    ScopedChatRetriever,
    ScopedChunk,
    build_chat_context,
    estimate_tokens,
    focused_retrieval_query,
    query_context_circular_ids,
    referenced_circular_ids,
)
from sbpeye.database import Base
from sbpeye.main import (
    _chat_turn_circular_ids,
    _truncate_chat_messages,
    get_chat_session,
)
from sbpeye.models import Attachment, ChatMessage, ChatSession, Circular
from sbpeye.search import SearchEngine, backfill_fts


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


def test_later_large_passage_uses_remaining_context_budget(monkeypatch):
    db = make_session()
    circular = add_circular(db, "one", "Circular body.")
    retriever = ScopedChatRetriever(db, [circular.id])
    retriever._chunks = [
        ScopedChunk(
            chunk_id="first",
            circular_id=circular.id,
            document_id=circular.id,
            document_type="circular",
            label="Circular one",
            text="a" * 1000,
            chunk_index=0,
        ),
        ScopedChunk(
            chunk_id="second",
            circular_id=circular.id,
            document_id="attachment-one",
            document_type="attachment",
            label="rules-one.pdf",
            text="needle value is PKR 3,000,000 " + ("b" * 2000),
            chunk_index=0,
        ),
    ]
    retriever._chunk_by_id = {
        chunk.chunk_id: chunk for chunk in retriever._chunks
    }

    class OrderedCollection:
        def query(self, **kwargs):
            return {
                "ids": [[
                    "first",
                    "second",
                ]],
                "metadatas": [[{}, {}]],
            }

    class FakeEmbeddings:
        def embed_queries(self, texts):
            return [[0.1]]

    import sbpeye.chat_retrieval as retrieval_module

    monkeypatch.setattr(retrieval_module, "collection", OrderedCollection())
    monkeypatch.setattr(retrieval_module, "embedding_backend", FakeEmbeddings())

    results = retriever.search("semantic", limit=2, token_budget=600)

    assert len(results) == 2
    assert "PKR 3,000,000" in results[1]["passage"]
    assert sum(estimate_tokens(item["passage"]) for item in results) <= 600


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


def test_explicit_unpinned_reference_is_added_only_to_turn_context():
    db = make_session()
    circular = Circular(
        id="unselected",
        reference="BPRD Circular Letter No. 09 of 2026",
        title="Customer Onboarding Framework",
        department="BPRD",
        date=datetime(2026, 3, 24),
        content_text="Circular body.",
    )
    db.add(circular)
    db.commit()

    message = "Check BPRD Circular Letter No. 09 of 2026 for the limit"

    assert referenced_circular_ids(db, message) == ["unselected"]
    pinned_ids: list[str] = []
    assert _chat_turn_circular_ids(db, pinned_ids, message) == ["unselected"]
    assert pinned_ids == []


def test_scoped_retrieval_removes_circular_reference_from_focus_query():
    query = (
        "Check BPRD Circular Letter No. 09 of 2026: "
        "what is the maximum credit balance for an Asaan Account?"
    )

    focused = focused_retrieval_query(query)

    assert "BPRD" not in focused
    assert "09" not in focused
    assert "maximum credit balance for an Asaan Account" in focused
    assert focused_retrieval_query("Is the limit PKR 300?") == "Is the limit PKR 300?"


def test_ambiguous_unpinned_reference_is_not_auto_selected():
    db = make_session()
    for circular_id, year in (("old", 2024), ("new", 2025)):
        db.add(Circular(
            id=circular_id,
            reference="BPRD Circular No. 04",
            title=f"Circular {year}",
            department="BPRD",
            date=datetime(year, 1, 1),
            content_text="Body.",
        ))
    db.commit()

    assert referenced_circular_ids(db, "Check BPRD Circular No. 04") == []


def test_latest_question_auto_scopes_newest_attachment_match(monkeypatch):
    disable_vectors(monkeypatch)
    import sbpeye.search as search_module

    monkeypatch.setattr(search_module, "collection", FailingCollection())
    monkeypatch.setattr(search_module, "embedding_backend", FailingEmbeddings())
    db = make_session()
    old = Circular(
        id="old",
        reference="BPRD Circular Letter No. 10 of 2022",
        title="Asaan Account Limits",
        department="BPRD",
        date=datetime(2022, 4, 13),
        content_text="Asaan Account maximum credit balance limit is PKR 1,000,000.",
    )
    new = Circular(
        id="new",
        reference="BPRD Circular Letter No. 09 of 2026",
        title="Consolidated Customer Onboarding Framework",
        department="BPRD",
        date=datetime(2026, 3, 24),
        content_text="The consolidated framework is attached.",
        attachments=[Attachment(
            id="new-framework",
            filename="updated-framework.pdf",
            original_url="https://www.sbp.org.pk/updated-framework.pdf",
            file_type="pdf",
            extraction_status="extracted",
            content_text=(
                "Asaan Account maximum credit balance limit is PKR 3,000,000."
            ),
        )],
    )
    db.add_all([old, new])
    db.commit()
    backfill_fts(db, force=True)

    query = "What is the latest maximum credit balance limit for Asaan Accounts?"
    inferred_ids = query_context_circular_ids(db, query)
    context, _ = build_chat_context(db, inferred_ids, query, 4000)

    assert inferred_ids == ["new"]
    assert _chat_turn_circular_ids(db, [], query) == ["new"]
    assert "PKR 3,000,000" in context


def test_circular_details_includes_relevant_attachment_context(monkeypatch):
    disable_vectors(monkeypatch)
    db = make_session()
    circular = add_circular(
        db,
        "one",
        "The circular introduces an updated framework.",
        ("background " * 500) + "Maximum credit balance is PKR 3,000,000.",
    )
    client = AIClient(AIConfig(max_context_tokens=800))

    payload = json.loads(client._execute_tool(
        "get_circular_details",
        {"circular_reference": circular.reference},
        db,
        user_query="What is the maximum credit balance?",
    ))

    assert "The circular introduces an updated framework." in payload["document_context"]
    assert "Maximum credit balance is PKR 3,000,000." in payload["document_context"]
    assert "[[attachment:attachment-one|rules-one.pdf]]" in payload["document_context"]


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
    search_module.backfill_fts(db)  # build the persistent lexical index

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


def test_reference_search_trusts_the_year_in_the_reference_over_the_date():
    """The cited year belongs to the reference *number*, not the publication date.

    SBP's EDMD circulars numbered "of 2002" through "of 2004" all carry a
    backfilled 2001-03-31 date, and "Circular No. 01 of 2011" is dated January
    2012. Narrowing on the date year alone made all 45 such circulars in the
    corpus unreachable by the reference their own listing prints.
    """
    db = make_session()
    db.add(
        Circular(
            id="backfilled",
            reference="EDMD Circular No. 12 of 2004",
            title="Export Finance Scheme",
            department="EDMD",
            date=datetime(2001, 3, 31),  # backfilled, disagrees with the reference
            content_text="Export refinance terms.",
        )
    )
    db.commit()

    results = SearchEngine._search_by_reference(
        "EDMD Circular No. 12 of 2004", db, limit=5
    )

    assert [item.id for item in results] == ["backfilled"]


def test_reference_search_separates_circulars_by_their_reference_year():
    """The year still discriminates — it is just read from the right place."""
    db = make_session()
    db.add_all([
        Circular(
            id="of-2003",
            reference="EDMD Circular No. 09 of 2003",
            title="Earlier scheme",
            department="EDMD",
            date=datetime(2001, 3, 31),
            content_text="Earlier terms.",
        ),
        Circular(
            id="of-2004",
            reference="EDMD Circular No. 09 of 2004",
            title="Later scheme",
            department="EDMD",
            date=datetime(2001, 3, 31),  # same backfilled date as the 2003 one
            content_text="Later terms.",
        ),
    ])
    db.commit()

    results = SearchEngine._search_by_reference(
        "EDMD Circular No. 09 of 2004", db, limit=5
    )

    assert [item.id for item in results] == ["of-2004"]


def test_reference_search_ignores_a_year_that_belongs_to_the_title():
    """A year elsewhere in the title must not decide the match.

    "FD Circular Letter No. 08 / 2018" spells its year with a slash, which
    REFERENCE_PATTERN does not read as the reference's year — so the date
    settles it. The title's own "Of 2016" must not be mistaken for the
    reference year and veto the match.
    """
    db = make_session()
    db.add(
        Circular(
            id="slashed",
            reference="FD Circular Letter No. 08 / 2018 of 2018",
            title="Constitution Petition No.57 Of 2016 - Dam Fund",
            department="FD",
            date=datetime(2018, 10, 4),
            content_text="Dam fund collections.",
        )
    )
    db.commit()

    results = SearchEngine._search_by_reference(
        "FD Circular Letter No. 08 of 2018", db, limit=5
    )

    assert [item.id for item in results] == ["slashed"]


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
