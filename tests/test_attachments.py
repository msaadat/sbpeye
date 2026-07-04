from datetime import datetime
from pathlib import Path
import json
import re
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


def test_detect_attachments_deduplicates_by_filename_across_urls():
    soup = BeautifulSoup(
        """
        <a href="https://www.sbp.org.pk/assets/document/FEC1-Annex-A.pdf">Annexure-I</a>
        <a href="https://www.sbp.org.pk/assets/documents/circulars/FEC1-Annex-A.pdf">Annexure-I</a>
        """,
        "html.parser",
    )

    found = scraper.detect_attachments(
        soup, "https://www.sbp.org.pk/circulars/fe-circular-no-01-of-2018"
    )

    assert found == [
        {
            "url": "https://www.sbp.org.pk/assets/document/FEC1-Annex-A.pdf",
            "filename": "FEC1-Annex-A.pdf",
            "file_type": "pdf",
        },
    ]


def test_detect_attachments_resolves_bare_filename_via_automation_path():
    soup = BeautifulSoup(
        """
        <span id="automationPathHolder" style="display:none;">/psd/2016/index.htm</span>
        <p>Encl: <a href="C3-Annexure-A.pdf">Annexure-A</a></p>
        """,
        "html.parser",
    )

    found = scraper.detect_attachments(
        soup, "https://www.sbp.org.pk/circulars/psd-circular-no-03-of-2016"
    )

    assert found == [
        {
            "url": "https://www.sbp.org.pk/assets/documents/circulars/psd/2016/C3-Annexure-A.pdf",
            "fallback_url": "https://www.sbp.org.pk/assets/documents/circulars/C3-Annexure-A.pdf",
            "filename": "C3-Annexure-A.pdf",
            "file_type": "pdf",
        },
    ]


def test_detect_attachments_resolves_bare_filename_without_automation_path():
    soup = BeautifulSoup('<a href="Foo-Annex.pdf">Foo</a>', "html.parser")

    found = scraper.detect_attachments(
        soup, "https://www.sbp.org.pk/circulars/some-circular"
    )

    assert found == [
        {
            "url": "https://www.sbp.org.pk/assets/documents/circulars/Foo-Annex.pdf",
            "filename": "Foo-Annex.pdf",
            "file_type": "pdf",
        },
    ]


def test_detect_attachments_relative_href_with_subdirectory_ignores_automation_path():
    soup = BeautifulSoup(
        """
        <span id="automationPathHolder" style="display:none;">/psd/2016/index.htm</span>
        <a href="files/rules.pdf?download=1">Rules</a>
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
    response = FakeResponse(b"%PDF document bytes")
    monkeypatch.setattr(scraper, "ATTACHMENTS_DIR", tmp_path)
    monkeypatch.setattr(scraper, "_get_sbp", lambda *args, **kwargs: response)
    info = {
        "url": "https://www.sbp.org.pk/files/report.pdf",
        "filename": "report.pdf",
        "file_type": "pdf",
    }

    path, downloaded, error, resolved_url = scraper.download_attachment("circ", info)

    assert error is None
    assert downloaded is True
    assert resolved_url == info["url"]
    assert path == tmp_path / "circ" / f"{scraper.attachment_id('circ', info['url'])}.pdf"
    assert path.read_bytes() == b"%PDF document bytes"
    assert response.closed is True
    assert not list(tmp_path.rglob("*.part"))


def test_download_attachment_falls_back_when_primary_content_is_html(monkeypatch, tmp_path):
    primary = "https://www.sbp.org.pk/assets/documents/circulars/psd/2016/C3-Annexure-A.pdf"
    fallback = "https://www.sbp.org.pk/assets/documents/circulars/C3-Annexure-A.pdf"
    responses = {
        primary: FakeResponse(b"<html>not a pdf</html>"),
        fallback: FakeResponse(b"%PDF real pdf bytes"),
    }
    monkeypatch.setattr(scraper, "ATTACHMENTS_DIR", tmp_path)
    monkeypatch.setattr(scraper, "_get_sbp", lambda url, **kwargs: responses[url])
    info = {
        "url": primary,
        "fallback_url": fallback,
        "filename": "C3-Annexure-A.pdf",
        "file_type": "pdf",
    }

    path, downloaded, error, resolved_url = scraper.download_attachment("circ", info)

    assert error is None
    assert downloaded is True
    assert resolved_url == fallback
    assert path.read_bytes() == b"%PDF real pdf bytes"
    assert not list(tmp_path.rglob("*.part"))


def test_download_attachment_reports_error_when_no_candidate_is_valid(monkeypatch, tmp_path):
    primary = "https://www.sbp.org.pk/assets/documents/circulars/psd/2016/Missing.pdf"
    fallback = "https://www.sbp.org.pk/assets/documents/circulars/Missing.pdf"
    responses = {
        primary: FakeResponse(b"<html>404</html>"),
        fallback: FakeResponse(b"<html>404</html>"),
    }
    monkeypatch.setattr(scraper, "ATTACHMENTS_DIR", tmp_path)
    monkeypatch.setattr(scraper, "_get_sbp", lambda url, **kwargs: responses[url])
    info = {
        "url": primary,
        "fallback_url": fallback,
        "filename": "Missing.pdf",
        "file_type": "pdf",
    }

    path, downloaded, error, resolved_url = scraper.download_attachment("circ", info)

    assert path is None
    assert downloaded is False
    assert error is not None
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
        lambda *args, **kwargs: (local_file, False, None, url),
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


def test_process_attachment_stores_resolved_fallback_url(monkeypatch, tmp_path):
    db = make_session()
    circular = make_circular()
    db.add(circular)
    db.commit()
    primary_url = "https://www.sbp.org.pk/assets/documents/circulars/psd/2016/C3-Annexure-A.pdf"
    fallback_url = "https://www.sbp.org.pk/assets/documents/circulars/C3-Annexure-A.pdf"
    local_file = tmp_path / "C3-Annexure-A.pdf"
    local_file.write_bytes(b"%PDF")
    monkeypatch.setattr(scraper, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        scraper,
        "download_attachment",
        lambda *args, **kwargs: (local_file, True, None, fallback_url),
    )
    monkeypatch.setattr(
        scraper,
        "extract_pdf_text",
        lambda path: ("Annexure text", "extracted", None),
    )

    result = scraper.process_attachment(
        db,
        circular,
        {
            "url": primary_url,
            "fallback_url": fallback_url,
            "filename": "C3-Annexure-A.pdf",
            "file_type": "pdf",
        },
    )

    assert result.original_url == fallback_url


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


def test_reextract_uses_local_cache_and_optionally_reindexes(monkeypatch, tmp_path):
    db = make_session()
    circular = make_circular()
    circular.compliance_checklist = '{"source_units": []}'
    attachment_path = tmp_path / "attachments" / "rules.pdf"
    attachment_path.parent.mkdir(parents=True)
    attachment_path.write_bytes(b"pdf")
    circular.attachments = [
        Attachment(
            id="attachment-1",
            circular_id=circular.id,
            filename="rules.pdf",
            original_url="https://www.sbp.org.pk/rules.pdf",
            local_path="attachments/rules.pdf",
            file_type="pdf",
            content_text="Old PDF text",
            extraction_status="extracted",
            is_vectorized=1,
        )
    ]
    db.add(circular)
    db.commit()

    html_cache = tmp_path / "html"
    html_cache.mkdir()
    cache_name = f"{uuid.uuid5(uuid.NAMESPACE_URL, circular.url)}.html"
    (html_cache / cache_name).write_bytes(
        b"<html><body><p>1. Banks shall report.</p></body></html>"
    )
    deleted = []
    indexed = []
    monkeypatch.setattr(scraper, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(scraper, "HTML_CACHE_DIR", html_cache)
    monkeypatch.setattr(
        scraper,
        "extract_pdf_text",
        lambda path: ("[[SBPEYE_PAGE:1]]\n2. Banks must retain records.", "extracted", None),
    )
    monkeypatch.setattr(
        scraper,
        "_delete_document_chunks",
        lambda **kwargs: deleted.append(kwargs),
    )
    monkeypatch.setattr(
        scraper,
        "_index_circular",
        lambda item, verbose=False: indexed.append(item.id),
    )
    monkeypatch.setattr(scraper, "vectorize_attachment", lambda *args, **kwargs: True)

    result = scraper.reextract_circular_from_cache(db, circular, reindex=True)

    assert result == {"changed": 2, "errors": 0, "indexed": 1}
    assert circular.content_text == "1. Banks shall report."
    assert circular.compliance_checklist is None
    assert circular.attachments[0].content_text.startswith("[[SBPEYE_PAGE:1]]")
    assert {tuple(value) for value in deleted} == {
        ("circular_id",),
        ("attachment_id_value",),
    }
    assert indexed == [circular.id]


def test_attachment_checklist_diagnostic_prints_full_verbose_trace(
    monkeypatch, capsys
):
    from sbpeye.ai import AIClient, AIConfig
    from sbpeye.cli.commands import _run_attachment_checklist

    db = make_session()
    circular = make_circular()
    circular.compliance_checklist = '{"existing": true}'
    circular.attachments = [
        Attachment(
            id="attachment-diagnostic",
            circular_id=circular.id,
            filename="diagnostic.pdf",
            original_url="https://www.sbp.org.pk/diagnostic.pdf",
            file_type="pdf",
            content_text="[[SBPEYE_PAGE:1]]\n1. Banks shall submit reports.",
            extraction_status="extracted",
        )
    ]
    db.add(circular)
    db.commit()
    client = AIClient(AIConfig())

    def complete(system, user, **kwargs):
        source_id = re.search(r"\[SOURCE_ID: ([^]]+)]", user).group(1)
        return json.dumps({"items": [{
            "requirement": "Banks shall submit reports.",
            "classification": "required",
            "source_unit_ids": [source_id],
        }]})

    monkeypatch.setattr(client, "_complete", complete)

    result = _run_attachment_checklist(
        db,
        client,
        attachment_id="attachment-diagnostic",
        verbose=True,
        delay=0,
    )
    output = capsys.readouterr().out

    assert output.index("=== 1. RAW CONTENT ===") < output.index("=== 2. DOCLING ITEMS")
    assert output.index("=== 2. DOCLING ITEMS") < output.index("=== 3. ANALYSIS BLOCKS")
    assert output.index("=== 3. ANALYSIS BLOCKS") < output.index("=== 4. LLM EXTRACTION ===")
    assert "--- RAW LLM OUTPUT: Page HTML" in output
    assert '"classification": "required"' in output
    assert "--- NORMALIZED BLOCK 1/1 ---" in output
    assert "=== 5. FINAL CHECKLIST ===" in output
    assert result["coverage_gaps"] == []
    assert result["checklist_items"][0]["classification"] == "required"
    assert circular.compliance_checklist == '{"existing": true}'


def test_attachment_dedupe_merges_same_filename_keeping_extracted(monkeypatch, tmp_path):
    import sbpeye.cli.commands as cli_commands
    from sbpeye.cli.commands import _run_attachment_dedupe

    db = make_session()
    circular = make_circular()
    keep_path = tmp_path / "keep.pdf"
    keep_path.write_bytes(b"keep")
    drop_path = tmp_path / "drop.pdf"
    drop_path.write_bytes(b"drop")
    circular.attachments = [
        Attachment(
            id="keep",
            circular_id=circular.id,
            filename="FEC1-Annex-A.pdf",
            original_url="https://www.sbp.org.pk/assets/document/FEC1-Annex-A.pdf",
            local_path="keep.pdf",
            file_type="pdf",
            content_text="Annexure text",
            extraction_status="extracted",
            created_at=datetime(2025, 1, 1),
        ),
        Attachment(
            id="drop",
            circular_id=circular.id,
            filename="fec1-annex-a.pdf",
            original_url="https://www.sbp.org.pk/assets/documents/circulars/FEC1-Annex-A.pdf",
            local_path="drop.pdf",
            file_type="pdf",
            content_text="Annexure text",
            extraction_status="extracted",
            created_at=datetime(2025, 1, 2),
        ),
    ]
    db.add(circular)
    db.commit()
    deleted_chunks = []
    monkeypatch.setattr(cli_commands, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        scraper, "_delete_document_chunks", lambda **kwargs: deleted_chunks.append(kwargs)
    )

    summary = _run_attachment_dedupe(db)

    remaining = db.query(Attachment).filter(Attachment.circular_id == circular.id).all()
    assert [item.id for item in remaining] == ["keep"]
    assert summary == {"groups": 1, "removed": 1}
    assert deleted_chunks == [{"attachment_id_value": "drop"}]
    assert not drop_path.exists()
    assert keep_path.exists()


def test_attachment_dedupe_prefers_extracted_over_error(monkeypatch):
    from sbpeye.cli.commands import _run_attachment_dedupe

    db = make_session()
    circular = make_circular()
    circular.attachments = [
        Attachment(
            id="broken",
            circular_id=circular.id,
            filename="Annex-B.pdf",
            original_url="https://www.sbp.org.pk/circulars/Annex-B.pdf",
            file_type="pdf",
            extraction_status="error",
            created_at=datetime(2025, 1, 1),
        ),
        Attachment(
            id="working",
            circular_id=circular.id,
            filename="Annex-B.pdf",
            original_url="https://www.sbp.org.pk/assets/documents/circulars/Annex-B.pdf",
            file_type="pdf",
            content_text="Annexure text",
            extraction_status="extracted",
            created_at=datetime(2025, 1, 2),
        ),
    ]
    db.add(circular)
    db.commit()
    monkeypatch.setattr(scraper, "_delete_document_chunks", lambda **kwargs: None)

    summary = _run_attachment_dedupe(db)

    remaining = db.query(Attachment).filter(Attachment.circular_id == circular.id).all()
    assert [item.id for item in remaining] == ["working"]
    assert summary == {"groups": 1, "removed": 1}


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
