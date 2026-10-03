"""`/api/laws/most_cited`: the landing page's "start here" ranking.

The count is the whole content of the card, so what it counts is pinned here: distinct
circulars, a part's citations credited to its container, and a circular-backed row's
edge to its own circular left out — that edge is SBP listing the circular *as* the
document, not citing it.
"""

from datetime import datetime

from sbpeye.models import Circular, RegDocument, RegDocumentLink

from test_laws_search import FE_TEXT, add_law, client  # noqa: F401 — pytest fixture


def add_circular(db, circular_id, date):
    circular = Circular(
        id=circular_id,
        reference=f"Circular {circular_id}",
        title=f"Circular {circular_id}",
        department="BPRD",
        date=date,
        url=f"https://www.sbp.org.pk/circulars/{circular_id}",
    )
    db.add(circular)
    db.commit()
    return circular


def cite(db, circular_id, document_id, link_type="references", detected_via="name_match"):
    db.add(RegDocumentLink(
        circular_id=circular_id, document_id=document_id,
        link_type=link_type, detected_via=detected_via,
    ))
    db.commit()


def test_ranks_by_distinct_citing_circulars(client):
    test_client, db = client
    add_law(db, "bco", title="Banking Companies Ordinance 1962", doc_type="law", index=False)
    add_law(db, "pr-sme", title="Prudential Regulations for SME Financing", index=False)
    add_law(db, "uncited", title="Data Revision Policy", doc_type="guideline", index=False)
    for index in range(3):
        add_circular(db, f"c{index}", datetime(2026, 1 + index, 1))
    for index in range(3):
        cite(db, f"c{index}", "bco")
    cite(db, "c0", "pr-sme")
    # The same circular found twice — by URL and by name — is one circular citing it.
    cite(db, "c0", "pr-sme", detected_via="url_scan")

    items = test_client.get("/api/laws/most_cited").json()["items"]

    assert [item["document"]["id"] for item in items] == ["bco", "pr-sme"]
    assert [item["circular_count"] for item in items] == [3, 1]
    assert items[0]["latest_cited_at"].startswith("2026-03-01")


def test_a_part_is_credited_to_its_container(client):
    test_client, db = client
    add_law(db, "fe-manual", title="Foreign Exchange Manual", file_type="manifest", index=False)
    for order in (12, 13):
        add_law(db, f"chapter-{order}", title=f"CHAPTER {order}", text_body=FE_TEXT,
                parent_id="fe-manual", part_label=f"Chapter {order}", part_order=order,
                index=False)
    add_circular(db, "c1", datetime(2026, 5, 1))
    add_circular(db, "c2", datetime(2026, 6, 1))
    # One circular revising two chapters cites the Manual once.
    cite(db, "c1", "chapter-12")
    cite(db, "c1", "chapter-13")
    cite(db, "c2", "fe-manual")

    items = test_client.get("/api/laws/most_cited").json()["items"]

    assert [item["document"]["id"] for item in items] == ["fe-manual"]
    assert items[0]["circular_count"] == 2
    assert items[0]["part_count"] == 2


def test_a_circular_backed_row_does_not_cite_itself(client):
    test_client, db = client
    add_circular(db, "own", datetime(2024, 10, 10))
    add_circular(db, "other", datetime(2025, 1, 1))
    db.add(RegDocument(id="blp", title="Branch Licensing Policy", doc_type="guideline",
                       circular_id="own", first_seen_at=datetime(2026, 8, 1)))
    db.commit()
    cite(db, "own", "blp", link_type="listing", detected_via="listing")
    cite(db, "own", "blp")  # the name pass finding the title in the circular itself
    cite(db, "other", "blp")

    items = test_client.get("/api/laws/most_cited").json()["items"]

    assert [(item["document"]["id"], item["circular_count"]) for item in items] == [("blp", 1)]


def test_delisted_documents_and_the_limit(client):
    test_client, db = client
    add_law(db, "gone", title="Withdrawn Guidelines", index=False,
            delisted_at=datetime(2026, 8, 15))
    for index in range(3):
        add_law(db, f"doc-{index}", title=f"Regulation {index}", index=False)
    add_circular(db, "c1", datetime(2026, 1, 1))
    for document_id in ("gone", "doc-0", "doc-1", "doc-2"):
        cite(db, "c1", document_id)

    items = test_client.get("/api/laws/most_cited", params={"limit": 2}).json()["items"]

    assert len(items) == 2
    assert "gone" not in {item["document"]["id"] for item in items}
