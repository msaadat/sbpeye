"""`/api/laws` must not read law text it does not send.

`_law_summary` reaches through four relationships per document, and one of them carries
`RegDocumentVersion.content_text` — the whole extracted PDF. None of it reaches the
response, so listing the corpus read 4.88 MB off disk across 273 queries to answer
questions as small as "how many versions are there" (docs/PERFORMANCE_PLAN.md P5).

Worth a test rather than leaving it to the benchmark, because the failure is silent: the
fix is loader options on a query, and a refactor that drops them leaves every other test
green while the endpoint quietly goes back to 273 queries. `benchmarks/perf_baseline.py
--section laws` measures the same thing in more detail, but nothing runs it automatically.
"""
from sqlalchemy import inspect as sa_inspect

from sbpeye.api.serializers import _law_summary, law_summary_load_options
from sbpeye.models import RegDocument

from test_laws_search import add_law, make_session


def seed(db, count=6):
    for index in range(count):
        add_law(
            db,
            document_id=f"doc-{index}",
            title=f"Regulation {index:02d} on Something",
            text_body=f"Body text for document {index}. " * 200,
            index=False,
        )


def test_listing_does_not_load_law_text():
    """The property P5 is about: the expensive column is never materialised."""
    db, _ = make_session()
    seed(db)

    documents = (
        db.query(RegDocument)
        .options(*law_summary_load_options())
        .order_by(RegDocument.title)
        .all()
    )
    payloads = [_law_summary(document) for document in documents]

    assert payloads, "the seed produced no documents, so this proved nothing"
    loaded = [
        version.id
        for document in documents
        for version in document.versions
        if "content_text" not in sa_inspect(version).unloaded
    ]
    assert not loaded, f"content_text was materialised for {loaded}"


def test_load_options_do_not_change_the_payload():
    """The safety half, and it guards a different mistake than the test above.

    Dropping the options entirely would not fail this — that is what the first test is
    for. What this catches is the options being *edited* into something that changes the
    result: a `joinedload` against a collection multiplies the parent rows, which would
    silently duplicate entries in the list. Easy to write, invisible until someone counts.
    """
    db, _ = make_session()
    seed(db)

    lazy = [_law_summary(d) for d in
            db.query(RegDocument).order_by(RegDocument.title).all()]
    db.expunge_all()
    preloaded = [_law_summary(d) for d in
                 db.query(RegDocument).options(*law_summary_load_options())
                 .order_by(RegDocument.title).all()]

    assert lazy == preloaded
