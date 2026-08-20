from conftest import TEST_ADMIN_ID
"""Whole-conversation markdown export.

The property under test throughout is that the file stands on its own: both sides of
the conversation are present, and every citation is either a working link to sbp.org.pk
or plain prose — never a dead link and never a leftover uuid.
"""

from datetime import datetime

import pytest

from sbpeye.chat_export import render_session_markdown, session_filename
from sbpeye.models import (
    Attachment,
    ChatMessage,
    ChatSession,
    Circular,
    RegDocument,
    RegDocumentVersion,
)


EXPORTED_AT = datetime(2026, 8, 19, 9, 30)


@pytest.fixture
def db(db_factory):
    session = db_factory()
    try:
        yield session
    finally:
        session.close()


def make_circular(db, circular_id="c-1", **overrides):
    fields = {
        "id": circular_id,
        "reference": "BPRD Circular No. 3 of 2026",
        "title": "Capital adequacy",
        "new_url": "https://www.sbp.org.pk/bprd/2026/C3.htm",
        "old_url": "https://archive.sbp.org.pk/bprd/2026/C3.htm",
    }
    fields.update(overrides)
    circular = Circular(**fields)
    db.add(circular)
    db.commit()
    return circular


def render(db, messages, title="Capital rules"):
    rows = [
        ChatMessage(
            id=f"m-{index}",
            session_id="s-1",
            role=role,
            content=content,
            created_at=datetime(2026, 8, 19, 9, index),
        )
        for index, (role, content) in enumerate(messages)
    ]
    return render_session_markdown(db, title, rows, exported_at=EXPORTED_AT)


def test_export_carries_both_sides_of_the_conversation(db):
    markdown = render(db, [("user", "What changed?"), ("assistant", "The ratio rose.")])

    assert markdown.startswith("# Capital rules\n")
    assert "Exported from SBPEye on 19 Aug 2026" in markdown
    assert "## You" in markdown and "What changed?" in markdown
    assert "## Assistant" in markdown and "The ratio rose." in markdown
    # Stated, because a transcript is read in another timezone and often much later.
    assert "*19 Aug 2026, 09:00 UTC*" in markdown


def test_circular_citation_becomes_a_link_to_the_live_page(db):
    make_circular(db)

    markdown = render(db, [("assistant", "See [[circular:c-1|BPRD Circular No. 3 of 2026]].")])

    assert (
        "See [BPRD Circular No. 3 of 2026](https://www.sbp.org.pk/bprd/2026/C3.htm)."
        in markdown
    )
    # The archive mirror is a fallback, never the address handed to a reader when the
    # redesigned page exists.
    assert "archive.sbp.org.pk" not in markdown


def test_circular_falls_back_through_the_url_columns(db):
    make_circular(db, new_url=None, url=None)

    markdown = render(db, [("assistant", "See [[circular:c-1|BPRD 3]].")])

    assert "[BPRD 3](https://archive.sbp.org.pk/bprd/2026/C3.htm)" in markdown


def test_law_citation_uses_the_landing_page_then_the_file(db):
    db.add(RegDocument(id="d-1", title="Banking Companies Ordinance", source_url="https://www.sbp.org.pk/l/bco.html"))
    db.add(RegDocument(id="d-2", title="AML Regulations", source_url=None))
    db.add(RegDocumentVersion(id="v-2", document_id="d-2", file_url="https://www.sbp.org.pk/l/aml.pdf", is_current=1))
    db.commit()

    markdown = render(
        db,
        [("assistant", "Per [[law:d-1|Banking Companies Ordinance]] and [[law:d-2|AML Regulations]].")],
    )

    assert "[Banking Companies Ordinance](https://www.sbp.org.pk/l/bco.html)" in markdown
    assert "[AML Regulations](https://www.sbp.org.pk/l/aml.pdf)" in markdown


def test_attachment_resolves_by_id_or_by_filename(db):
    make_circular(db)
    db.add(Attachment(
        id="a-1",
        circular_id="c-1",
        filename="Annexure A.pdf",
        original_url="https://www.sbp.org.pk/bprd/2026/Annexure A.pdf",
    ))
    db.commit()

    markdown = render(
        db,
        [("assistant", "See [[attachment:a-1|Annexure A]] and [[attachment:Annexure A.pdf|the annexure]].")],
    )

    # A URL carrying spaces only survives markdown inside angle brackets.
    assert "[Annexure A](<https://www.sbp.org.pk/bprd/2026/Annexure A.pdf>)" in markdown
    assert "[the annexure](<https://www.sbp.org.pk/bprd/2026/Annexure A.pdf>)" in markdown


def test_unresolvable_citation_degrades_to_prose_rather_than_a_dead_link(db):
    markdown = render(db, [("assistant", "See [[circular:gone|BPRD Circular No. 9]] for detail.")])

    assert "See BPRD Circular No. 9 for detail." in markdown
    assert "](" not in markdown


def test_unlabelled_citation_takes_the_label_from_the_database(db):
    make_circular(db)

    markdown = render(db, [("assistant", "See [[circular:c-1]].")])

    assert "[BPRD Circular No. 3 of 2026](https://www.sbp.org.pk/bprd/2026/C3.htm)" in markdown


def test_bare_uuid_is_linked_when_real_and_dropped_when_not(db):
    real = "da13ed0e-96c5-5945-adb2-1b2c3d4e5f60"
    invented = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    make_circular(db, circular_id=real)

    markdown = render(db, [("assistant", f"See {real} but not {invented}.")])

    assert "[BPRD Circular No. 3 of 2026](https://www.sbp.org.pk/bprd/2026/C3.htm)" in markdown
    assert invented not in markdown
    assert "but not." in markdown


def test_a_uuid_the_user_typed_is_left_alone(db):
    invented = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    markdown = render(db, [("user", f"What is {invented}?")])

    assert invented in markdown


def test_empty_conversation_still_renders_a_file(db):
    markdown = render(db, [])

    assert "# Capital rules" in markdown
    assert "no messages yet" in markdown


def test_filename_is_derived_from_the_title_and_stays_filesystem_safe():
    assert session_filename("Capital: rules & ratios", exported_at=EXPORTED_AT) == (
        "sbpeye-chat-Capital-rules-ratios-2026-08-19.md"
    )
    assert session_filename(None, exported_at=EXPORTED_AT) == "sbpeye-chat-session-2026-08-19.md"
    assert session_filename("///", exported_at=EXPORTED_AT) == "sbpeye-chat-session-2026-08-19.md"


def test_export_route_serves_a_markdown_attachment(client):
    test_client, db_factory = client
    db = db_factory()
    make_circular(db)
    db.add(ChatSession(user_id=TEST_ADMIN_ID, id="s-1", title="Capital rules"))
    db.add(ChatMessage(id="m-0", session_id="s-1", role="user", content="What changed?",
                       created_at=datetime(2026, 8, 19, 9, 0)))
    db.add(ChatMessage(id="m-1", session_id="s-1", role="assistant",
                       content="See [[circular:c-1|BPRD Circular No. 3 of 2026]].",
                       created_at=datetime(2026, 8, 19, 9, 1)))
    db.commit()
    db.close()

    response = test_client.get("/api/chat/sessions/s-1/export.md")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "attachment; filename=" in response.headers["content-disposition"]
    assert "sbpeye-chat-Capital-rules-" in response.headers["content-disposition"]
    body = response.text
    assert "## You" in body and "## Assistant" in body
    assert "(https://www.sbp.org.pk/bprd/2026/C3.htm)" in body


def test_export_route_404s_on_an_unknown_session(client):
    test_client, _ = client

    assert test_client.get("/api/chat/sessions/missing/export.md").status_code == 404
