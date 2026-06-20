from datetime import datetime
from pathlib import Path
import uuid

from bs4 import BeautifulSoup
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sbpeye.database import Base
from sbpeye.documents import build_corpus
from sbpeye.models import Attachment, Circular
from sbpeye.scraper import circulars as scraper
from sbpeye.search import SearchEngine


class FakeResponse:
    def __init__(self, content: bytes):
        self.content = content
        self.closed = False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size: int):
        midpoint = max(len(self.content) // 2, 1)
        yield self.content[:midpoint]
        yield self.content[midpoint:]

    def close(self):
        self.closed = True


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def make_circular(circular_id: str = "circular-1") -> Circular:
    return Circular(
        id=circular_id,
        title="Test circular",
        department="BPRD",
        date=datetime(2025, 1, 1),
        url="https://www.sbp.org.pk/circular.htm",
        content_text="Circular body text",
    )


def test_detect_attachments_deduplicates_and_skips_urdu():
    soup = BeautifulSoup(
        """
        <a href="files/rules.pdf?download=1">Rules</a>
        <a href="files/rules.pdf?download=1">Duplicate</a>
        <a href="files/rules-u.pdf">Urdu</a>
        <a href="files/report.xlsx#sheet">Report</a>
        <a href="notes.txt">Notes</a>
        <a href="https://example.com/external.pdf">External</a>
        <a href="https://user@sbp.org.pk/credentialed.pdf">Credentialed</a>
        """,
        "html.parser",
    )

    found = scraper.detect_attachments(
        soup, "https://www.sbp.org.pk/circulars/2025/page.htm"
    )

    assert found == [
        {
            "url": "https://www.sbp.org.pk/circulars/2025/files/rules.pdf?download=1",
            "filename": "rules.pdf",
            "file_type": "pdf",
        },
        {
            "url": "https://www.sbp.org.pk/circulars/2025/files/report.xlsx",
            "filename": "report.xlsx",
            "file_type": "xlsx",
        },
    ]


def test_attachment_id_is_scoped_to_circular():
    url = "https://www.sbp.org.pk/files/rules.pdf"
    assert scraper.attachment_id("one", url) == scraper.attachment_id("one", url)
    assert scraper.attachment_id("one", url) != scraper.attachment_id("two", url)


def test_fetch_page_cached_uses_uuid_filename(monkeypatch, tmp_path):
    response = FakeResponse(b"<html>cached</html>")
    calls = []

    def fake_get(*args, **kwargs):
        calls.append(args[0])
        return response

    monkeypatch.setattr(scraper, "HTML_CACHE_DIR", tmp_path)
    monkeypatch.setattr(scraper.requests, "get", fake_get)
    url = "https://www.sbp.org.pk/circular.htm"

    assert scraper.fetch_page_cached(url) == response.content
    assert scraper.fetch_page_cached(url) == response.content
    assert calls == [url]
    expected = tmp_path / f"{uuid.uuid5(uuid.NAMESPACE_URL, url)}.html"
    assert expected.read_bytes() == response.content
    assert not list(tmp_path.glob("*.part"))


def test_download_attachment_streams_to_id_based_path(monkeypatch, tmp_path):
    response = FakeResponse(b"document bytes")
    monkeypatch.setattr(scraper, "ATTACHMENTS_DIR", tmp_path)
    monkeypatch.setattr(scraper.requests, "get", lambda *args, **kwargs: response)
    info = {
        "url": "https://www.sbp.org.pk/files/report.pdf",
        "filename": "report.pdf",
        "file_type": "pdf",
    }

    path, downloaded, error = scraper.download_attachment("circ", info)

    assert error is None
    assert downloaded is True
    assert path == tmp_path / "circ" / f"{scraper.attachment_id('circ', info['url'])}.pdf"
    assert path.read_bytes() == b"document bytes"
    assert response.closed is True
    assert not list(tmp_path.rglob("*.part"))


def test_process_attachment_retries_error_and_sets_extracted(monkeypatch, tmp_path):
    db = make_session()
    circular = make_circular()
    db.add(circular)
    db.commit()
    url = "https://www.sbp.org.pk/files/report.pdf"
    attachment = Attachment(
        id=scraper.attachment_id(circular.id, url),
        circular_id=circular.id,
        filename="report.pdf",
        original_url=url,
        file_type="pdf",
        extraction_status="error",
        extraction_error="temporary failure",
    )
    db.add(attachment)
    db.commit()
    local_file = tmp_path / "report.pdf"
    local_file.write_bytes(b"pdf")
    monkeypatch.setattr(scraper, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        scraper,
        "download_attachment",
        lambda *args, **kwargs: (local_file, False, None),
    )
    monkeypatch.setattr(
        scraper,
        "extract_pdf_text",
        lambda path: ("Extracted requirements", "extracted", None),
    )

    result = scraper.process_attachment(
        db,
        circular,
        {"url": url, "filename": "report.pdf", "file_type": "pdf"},
    )

    assert result.extraction_status == "extracted"
    assert result.extraction_error is None
    assert result.content_text == "Extracted requirements"
    assert result.local_path == "report.pdf"
    assert result.is_vectorized == 0


def test_build_corpus_orders_circular_then_named_attachments():
    circular = make_circular()
    circular.attachments = [
        Attachment(
            id="b",
            circular_id=circular.id,
            filename="zeta.pdf",
            original_url="https://example/zeta.pdf",
            content_text="Zeta",
            file_type="pdf",
        ),
        Attachment(
            id="a",
            circular_id=circular.id,
            filename="alpha.pdf",
            original_url="https://example/alpha.pdf",
            content_text="Alpha",
            file_type="pdf",
        ),
    ]

    corpus = build_corpus(circular)

    assert [item["doc_id"] for item in corpus] == [circular.id, "a", "b"]


def test_search_uses_attachment_for_bm25_and_snippet(monkeypatch):
    db = make_session()
    circular = make_circular()
    circular.attachments = [
        Attachment(
            id="attachment-1",
            circular_id=circular.id,
            filename="requirements.pdf",
            original_url="https://example/requirements.pdf",
            content_text="Institutions must submit quuxreport every quarter.",
            file_type="pdf",
            extraction_status="extracted",
        )
    ]
    db.add(circular)
    db.commit()

    class EmptyCollection:
        def query(self, **kwargs):
            return {"ids": [[]], "metadatas": [[]]}

    class FakeEmbeddings:
        def embed_queries(self, texts):
            return [[0.0]]

    import sbpeye.search as search_module

    monkeypatch.setattr(search_module, "collection", EmptyCollection())
    monkeypatch.setattr(search_module, "embedding_backend", FakeEmbeddings())
    engine = SearchEngine()

    results, total = engine.search("quuxreport", db)

    assert total == 1
    assert results[0]["match_source"] == "attachment"
    assert results[0]["attachment_id"] == "attachment-1"
    assert results[0]["attachment_filename"] == "requirements.pdf"
    assert "<mark>quuxreport</mark>" in results[0]["snippet"]


def test_vectorize_attachment_writes_source_metadata(monkeypatch):
    db = make_session()
    circular = make_circular()
    attachment = Attachment(
        id="attachment-1",
        circular_id=circular.id,
        filename="requirements.pdf",
        original_url="https://example/requirements.pdf",
        content_text="A specific reporting requirement.",
        file_type="pdf",
        extraction_status="extracted",
    )
    circular.attachments = [attachment]
    db.add(circular)
    db.commit()

    class FakeCollection:
        def __init__(self):
            self.added = None

        def get(self, **kwargs):
            return {"ids": [], "metadatas": []}

        def add(self, **kwargs):
            self.added = kwargs

    class FakeEmbeddings:
        def embed_documents(self, texts):
            return [[0.1] for _ in texts]

    collection = FakeCollection()
    monkeypatch.setattr(scraper, "collection", collection)
    monkeypatch.setattr(scraper, "embedding_backend", FakeEmbeddings())

    assert scraper.vectorize_attachment(db, attachment) is True
    assert attachment.is_vectorized == 1
    assert collection.added["embeddings"]
    assert collection.added["metadatas"][0]["doc_type"] == "attachment"
    assert collection.added["metadatas"][0]["attachment_id"] == attachment.id


def test_attachment_vectorize_by_id_reindexes_only_selected(monkeypatch):
    from sbpeye.cli.commands import _run_attachment_vectorize

    db = make_session()
    circular = make_circular()
    circular.attachments = [
        Attachment(
            id="selected",
            circular_id=circular.id,
            filename="selected.pdf",
            original_url="https://example/selected.pdf",
            content_text="Selected attachment text",
            is_vectorized=1,
        ),
        Attachment(
            id="other",
            circular_id=circular.id,
            filename="other.pdf",
            original_url="https://example/other.pdf",
            content_text="Other attachment text",
            is_vectorized=0,
        ),
    ]
    db.add(circular)
    db.commit()
    vectorized = []

    monkeypatch.setattr(
        scraper,
        "vectorize_attachment",
        lambda db, attachment, verbose=False: vectorized.append(attachment.id) or True,
    )

    indexed = _run_attachment_vectorize(db, attachment_id="selected")

    assert indexed == 1
    assert vectorized == ["selected"]
