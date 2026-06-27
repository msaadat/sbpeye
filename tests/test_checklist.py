import json
import re
from types import SimpleNamespace

import pytest

from sbpeye.ai import AIClient, AIConfig
from sbpeye.checklist import (
    AnalysisBlock,
    ReferenceUnit,
    _expand_marker,
    automatic_na_reason,
    build_analysis_blocks,
    build_checklist_corpus,
    build_extraction_batches,
    compact_required_checklist,
    prepare_reference_chunks,
    segment_document,
)
from sbpeye.scraper.clean_html import clean_sbp_html, extract_sbp_text
from sbpeye.scraper.circulars import _clean_pdf_pages


def make_circular(content: str, attachments=None):
    return SimpleNamespace(
        id="circular-1",
        title="Reporting requirements",
        reference="BPRD Circular No. 1",
        department="BPRD",
        content_text=content,
        attachments=attachments or [],
    )


def test_html_extraction_preserves_blocks_and_inline_text():
    text = extract_sbp_text(b"""
        <html><body>
          <h1>Reporting Rules</h1>
          <p>1. Banks <strong>shall</strong> report monthly.</p>
          <p>2. Records must be retained.</p>
        </body></html>
    """)

    assert "Banks shall report" in text
    assert "Reporting Rules\n\n1. Banks" in text
    assert "monthly.\n\n2. Records" in text


def test_html_extraction_removes_known_sbp_navigation_footer():
    text = extract_sbp_text(b"""
        <html><body>
          <p>1. Banks shall report.</p>
          <div>Home About SBP Publications Economic Data Press Releases
          Circulars/Notifications Laws &amp; Regulations Best view Screen Resolution :
          1024 * 768 Copyright 2016. All Rights Reserved.</div>
        </body></html>
    """)

    assert text == "1. Banks shall report."


def test_clean_html_removes_back_to_circular_and_home_row():
    cleaned = clean_sbp_html(b"""
        <html><body>
          <table>
            <tr><td><p>1. Banks shall report monthly to the State Bank of Pakistan and retain supporting records for inspection by supervisors and internal audit teams.</p></td></tr>
            <tr><td><p>2. The submission must include transaction summaries, exception reports, account-level reconciliations, evidence of management review, and any remediation steps taken after control failures so the circular body remains substantially longer than the navigation footer.</p></td></tr>
            <tr>
              <td>
                <a href="../../circulars/index.asp"><strong>Back to Circular Page</strong></a>
                <strong>/</strong>
                <a href="../index.htm"><strong>Home Page</strong></a>
              </td>
            </tr>
          </table>
        </body></html>
    """)

    assert "Back to Circular Page" not in cleaned
    assert "Home Page" not in cleaned
    assert "Banks shall report monthly" in cleaned


def test_pdf_page_cleaning_removes_repeated_furniture_and_dehyphenates():
    pages = _clean_pdf_pages([
        "Rules Manual\n1. Banks shall meet require-\nments.\n1",
        "Rules Manual\n2. Banks must report.\n2",
        "Rules Manual\n3. Banks may file online.\n3",
    ])

    assert pages[0] == "1. Banks shall meet requirements."
    assert all("Rules Manual" not in page for page in pages)


def test_docling_parsing_preserves_heading_reference_and_source_text():
    document = {
        "doc_id": "attachment-1",
        "doc_type": "attachment",
        "doc_label": "rules.pdf",
        "file_type": "html",
        "text": (
            "# CAPITAL REQUIREMENTS\n\n"
            "1. Banks shall maintain capital.\n"
            "2. Banks may use eligible instruments."
        ),
    }

    units = segment_document(document)

    assert [unit.ref for unit in units] == [
        "Section CAPITAL REQUIREMENTS > Para 1",
        "Section CAPITAL REQUIREMENTS > Para 2",
    ]
    assert all(unit.page_start is None for unit in units)
    assert units[0].heading_path == ["CAPITAL REQUIREMENTS"]
    assert units[0].source_text == "Banks shall maintain capital."
    assert units[0].unit_id == segment_document(document)[0].unit_id

    chunks = prepare_reference_chunks(document)
    assert chunks[0]["ref"] == "Chunk 1"
    assert chunks[0]["page_start"] is None
    assert chunks[0]["text"].startswith("rules.pdf. Chunk 1.")


def test_docling_parsing_uses_structural_nested_list_paths():
    document = {
        "doc_id": "attachment-1",
        "doc_type": "attachment",
        "doc_label": "rules.pdf",
        "file_type": "pdf",
        "text": """1. First duty.
   1. Nested duty.
   2. Second nested duty.
2. Final duty.""",
    }

    assert [unit.ref for unit in segment_document(document)] == [
        "Para 1",
        "Para 1 > Sub-para 1",
        "Para 1 > Sub-para 2",
        "Para 2",
    ]


def test_flattened_legal_markers_expand_into_hierarchy():
    assert _expand_marker("2.3.1") == ["2", "3", "1"]
    assert _expand_marker("2.3(a)") == ["2", "3", "(a)"]
    assert _expand_marker("R-2.3") == ["R-2.3"]


def test_docling_parsing_assigns_deterministic_paragraph_references():
    document = {
        "doc_id": "attachment-1",
        "doc_type": "attachment",
        "doc_label": "rules.pdf",
        "file_type": "pdf",
        "text": "Submit reports online through the portal.\n\nRetain the receipt.",
    }

    units = segment_document(document)

    assert [unit.ref for unit in units] == ["Para 1", "Para 2"]


def test_plain_form_fields_are_not_promoted_to_headings():
    document = {
        "doc_id": "attachment-1",
        "doc_type": "attachment",
        "doc_label": "form.pdf",
        "file_type": "pdf",
        "text": "The Chief Manager, Dated: _____________\n\n1. Name of institution.",
    }

    units = segment_document(document)

    assert all("The Chief Manager" not in unit.ref for unit in units)


def test_artifact_heuristics_identify_templates_and_table_headers():
    assert automatic_na_reason("Reporting month <MM-YYYY>: __________") == "template_placeholder"
    assert automatic_na_reason(
        "General format Accounting format ISO 3166-1 General format"
    ) == "table_header"
    assert automatic_na_reason("Banks shall report in ISO 3166-1 format.") is None


def test_checklist_corpus_uses_cached_pdfs_and_reports_missing_files(tmp_path):
    pdf_path = tmp_path / "rules.pdf"
    pdf_path.write_bytes(b"%PDF placeholder")
    attachments = [
        SimpleNamespace(
            id="pdf-ok", filename="rules.pdf", file_type="pdf",
            content_text="1. Banks shall report.", extraction_status="extracted",
            extraction_error=None, local_path=str(pdf_path),
        ),
        SimpleNamespace(
            id="pdf-scan", filename="scan.pdf", file_type="pdf",
            content_text=None, extraction_status="scanned", extraction_error=None,
            local_path=None,
        ),
        SimpleNamespace(
            id="sheet", filename="format.xlsx", file_type="xlsx",
            content_text="Headers", extraction_status="extracted", extraction_error=None,
            local_path=None,
        ),
    ]

    documents, gaps = build_checklist_corpus(make_circular("Circular body.", attachments))

    assert [item["doc_id"] for item in documents] == ["circular-1", "pdf-ok"]
    assert gaps == [{
        "doc_id": "pdf-scan",
        "doc_type": "attachment",
        "doc_label": "scan.pdf",
        "reason": "missing_file",
        "error": None,
    }]


def test_analysis_blocks_group_complete_sections():
    document = {
        "doc_id": "attachment-1",
        "doc_type": "attachment",
        "doc_label": "rules.pdf",
        "file_type": "pdf",
        "text": (
            "# REPORTING\n\n"
            "1. Banks shall submit reports.\n"
            "2. Banks shall retain receipts.\n\n"
            "# GOVERNANCE\n\n"
            "1. Boards must approve the policy."
        ),
    }

    blocks = build_analysis_blocks(segment_document(document))

    assert [block.ref for block in blocks] == [
        "Section REPORTING",
        "Section GOVERNANCE",
    ]
    assert len(blocks[0].source_unit_ids) == 2
    assert "[SOURCE_ID:" in blocks[0].source_text


def test_generate_checklist_extracts_items_from_blocks_and_reports_progress(monkeypatch):
    client = AIClient(AIConfig())

    def complete(system, user, **kwargs):
        source_ids = re.findall(r"\[SOURCE_ID: ([^]]+)]", user)
        return json.dumps({"items": [
            {
                "requirement": "Banks must submit monthly reports and retain records.",
                "classification": "required",
                "actor": "Banks",
                "action": "submit and retain",
                "object": "monthly reports and records",
                "source_unit_ids": [source_ids[0]],
            },
            {
                "requirement": "Banks may file reports electronically.",
                "classification": "optional",
                "actor": "Banks",
                "action": "file",
                "object": "reports electronically",
                "source_unit_ids": [source_ids[-1]],
            },
        ]})

    monkeypatch.setattr(client, "_complete", complete)
    progress = []
    trace_events = []
    circular = make_circular(
        "1. Banks shall submit monthly reports and must retain records.\n\n"
        "2. Banks may file electronically."
    )

    result = client.generate_checklist(
        circular,
        progress_callback=lambda completed, total: progress.append((completed, total)),
        trace_callback=lambda event, payload: trace_events.append(event),
    )

    assert result["status"] == "completed"
    assert result["schema_version"] == 2
    assert result["coverage_gaps"] == []
    assert [item["classification"] for item in result["checklist_items"]] == ["required", "optional"]
    assert all(item["source_refs"] for item in result["checklist_items"])
    assert progress == [(0, 1), (1, 1)]
    assert trace_events == [
        "document",
        "parsing",
        "analysis_blocks",
        "llm_input", "llm_output", "normalized_block",
    ]


def test_generate_checklist_skips_llm_for_known_artifacts(monkeypatch):
    client = AIClient(AIConfig())
    monkeypatch.setattr(
        client,
        "_complete",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("LLM called")),
    )

    result = client.generate_checklist(
        make_circular("Reporting month <MM-YYYY>: __________")
    )

    assert result["checklist_items"] == []
    assert result["analysis_blocks"] == []
    assert result["source_units"][0]["source_text"].startswith("Reporting month")


def test_checklist_extraction_normalizes_fenced_and_embedded_json():
    valid_ids = {"source-1"}
    responses = [
        '```json\n{"items":[{"requirement":"Banks shall report monthly.","classification":"required","source_unit_ids":["source-1"]}]}\n```',
        'Result: {"items":[{"requirement":"Banks may report online.","classification":"optional","source_unit_ids":["source-1"]}]}',
    ]

    assert [
        AIClient._parse_checklist_items(response, valid_ids)[0]["classification"]
        for response in responses
    ] == ["required", "optional"]


def test_checklist_extraction_rejects_unknown_source_citations():
    response = json.dumps({"items": [{
        "requirement": "Banks shall report monthly.",
        "classification": "required",
        "source_unit_ids": ["invented-source"],
    }]})

    with pytest.raises(ValueError, match="valid source citation"):
        AIClient._parse_checklist_items(response, {"source-1"})


def test_checklist_extraction_accepts_citations_alias():
    # Models in json_object mode (no schema enforcement) sometimes return the
    # citation list under "citations" instead of "source_unit_ids".
    response = json.dumps({"items": [{
        "requirement": "Banks shall report monthly.",
        "classification": "required",
        "citations": ["source-1"],
    }]})

    items = AIClient._parse_checklist_items(response, {"source-1"})
    assert items[0]["source_unit_ids"] == ["source-1"]


def test_checklist_system_prompt_specifies_required_keys():
    # The prompt is the contract in json_object/text mode (schema not enforced),
    # so it must name the keys the parser requires or every response fails.
    prompt = AIClient._CHECKLIST_SYSTEM_PROMPT
    assert "requirement" in prompt
    assert "source_unit_ids" in prompt


def test_invalid_checklist_response_is_retried(monkeypatch):
    client = AIClient(AIConfig())
    responses = iter(["The unit is required.", None])

    def complete(system, user, **kwargs):
        response = next(responses)
        if response is not None:
            return response
        source_id = re.search(r"\[SOURCE_ID: ([^]]+)]", user).group(1)
        return json.dumps({"items": [{
            "requirement": "Banks shall report monthly.",
            "classification": "required",
            "source_unit_ids": [source_id],
        }]})

    monkeypatch.setattr(client, "_complete", complete)

    result = client.generate_checklist(make_circular("1. Banks shall report."))

    assert result["status"] == "completed"
    assert result["checklist_items"][0]["classification"] == "required"


def test_invalid_block_response_is_recorded_as_coverage_gap(monkeypatch):
    client = AIClient(AIConfig())
    monkeypatch.setattr(client, "_complete", lambda *args, **kwargs: "not json")

    result = client.generate_checklist(make_circular("1. Banks shall report."))
    assert result["checklist_items"] == []
    assert result["source_units"][0]["source_text"] == "Banks shall report."
    assert result["coverage_gaps"][0]["reason"] == "checklist_extraction_error"
    assert "First: 'not json'" in result["coverage_gaps"][0]["error"]
    assert result["status"] == "completed_with_gaps"


def test_scanned_pdf_marks_completed_checklist_as_having_gaps(monkeypatch):
    client = AIClient(AIConfig())
    monkeypatch.setattr(
        client,
        "_complete",
        lambda *args, **kwargs: json.dumps({"items": []}),
    )
    scanned = SimpleNamespace(
        id="scan", filename="scan.pdf", file_type="pdf", content_text=None,
        extraction_status="scanned", extraction_error=None, local_path=None,
    )

    result = client.generate_checklist(make_circular("Background information.", [scanned]))

    assert result["status"] == "completed_with_gaps"
    assert result["coverage_gaps"][0]["reason"] == "missing_file"


def test_docling_conversion_failure_is_reported_as_coverage_gap(monkeypatch):
    client = AIClient(AIConfig())
    monkeypatch.setattr(
        "sbpeye.checklist.segment_document",
        lambda document: (_ for _ in ()).throw(RuntimeError("conversion failed")),
    )

    result = client.generate_checklist(make_circular("Banks shall report."))

    assert result["status"] == "completed_with_gaps"
    assert result["source_units"] == []
    assert result["coverage_gaps"][0]["reason"] == "docling_conversion_error"
    assert result["coverage_gaps"][0]["error"] == "conversion failed"


def test_compact_checklist_prefers_normalized_required_items():
    value = {
        "checklist_items": [
            {"ref": "2.1", "doc_label": "rules.pdf", "classification": "required", "requirement": "File reports."},
            {"ref": "2.2", "doc_label": "rules.pdf", "classification": "optional", "requirement": "Use the portal."},
        ],
        "source_units": [],
    }

    assert compact_required_checklist(value) == [{
        "ref": "2.1",
        "doc_label": "rules.pdf",
        "requirement": "File reports.",
    }]


def test_compact_checklist_supports_legacy_required_source_units():
    value = {
        "source_units": [
            {"ref": "2.1", "doc_label": "rules.pdf", "classification": "required", "source_text": "File reports."},
            {"ref": "2.2", "doc_label": "rules.pdf", "classification": "optional", "source_text": "Use the portal."},
        ],
    }

    assert compact_required_checklist(value) == [{
        "ref": "2.1",
        "doc_label": "rules.pdf",
        "requirement": "File reports.",
    }]


# --- Batching, capability tiers, and degradation -------------------------------


def _make_block(block_id, text="Banks shall report monthly.", source_unit_ids=None):
    return AnalysisBlock(
        block_id=block_id,
        ref=block_id,
        doc_id="doc-1",
        doc_type="circular",
        doc_label="Doc",
        block_type="regulation",
        source_text=text,
        source_unit_ids=source_unit_ids or [block_id],
        heading_path=[],
        page_start=None,
        page_end=None,
    )


def _make_unit(unit_id, text="Banks shall report monthly."):
    return ReferenceUnit(
        unit_id=unit_id,
        ref=f"ref-{unit_id}",
        doc_id="doc-1",
        doc_type="circular",
        doc_label="Doc",
        source_text=text,
        heading_path=[],
        page_start=1,
        page_end=1,
        start_offset=0,
        end_offset=len(text),
        oversized=False,
        kind="text",
    )


def test_build_extraction_batches_packs_small_corpus_into_single_batch():
    blocks = [_make_block(f"b{i}", "short text") for i in range(3)]
    batches = build_extraction_batches(blocks, 1000, AIClient._estimate_tokens)
    assert len(batches) == 1
    assert [block.block_id for block in batches[0]] == ["b0", "b1", "b2"]


def test_build_extraction_batches_splits_when_over_budget():
    # Each block is ~100 estimated tokens (400 chars); budget fits two per batch.
    blocks = [_make_block(f"b{i}", "x" * 400) for i in range(3)]
    batches = build_extraction_batches(blocks, 250, AIClient._estimate_tokens)
    assert [[block.block_id for block in batch] for batch in batches] == [
        ["b0", "b1"],
        ["b2"],
    ]


def test_build_extraction_batches_isolates_oversized_block():
    big = _make_block("big", "x" * 4000)  # ~1000 tokens, over budget on its own
    small = _make_block("small", "x" * 40)
    batches = build_extraction_batches([big, small], 250, AIClient._estimate_tokens)
    assert [[block.block_id for block in batch] for batch in batches] == [
        ["big"],
        ["small"],
    ]


class _FakeBadRequest(Exception):
    status_code = 400


def _fake_response(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _install_fake_client(client, create):
    client._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )


def test_complete_downgrades_structured_mode_on_response_format_error():
    client = AIClient(AIConfig())
    client._structured_mode = "json_schema"
    calls = []

    def create(**kwargs):
        calls.append(kwargs.get("response_format"))
        rf = kwargs.get("response_format") or {}
        if rf.get("type") == "json_schema":
            raise _FakeBadRequest("response_format json_schema is not supported")
        return _fake_response('{"items": []}')

    _install_fake_client(client, create)
    result = client._complete("sys", "user", json_schema={"type": "object"})

    assert result == '{"items": []}'
    assert client._structured_mode == "json_object"
    assert [rf and rf.get("type") for rf in calls] == ["json_schema", "json_object"]


def test_complete_json_downgrades_when_model_ignores_structured_output():
    client = AIClient(AIConfig())
    client._structured_mode = "json_object"

    def create(**kwargs):
        # json_object mode returns prose; plain-text mode returns valid JSON.
        if kwargs.get("response_format"):
            return _fake_response("Sorry, here is the answer in prose.")
        return _fake_response('{"items": []}')

    _install_fake_client(client, create)
    result = client._complete_json("sys", "user", json_schema={"type": "object"})

    assert result == '{"items": []}'
    assert client._structured_mode == "text"


def test_run_checklist_batch_recovers_by_splitting_failed_batch(monkeypatch):
    client = AIClient(AIConfig())

    def fake_extract(*, circular_label, blocks, trace_callback=None):
        if len(blocks) > 1:
            raise ValueError("batch too large for one call")
        unit_id = blocks[0].source_unit_ids[0]
        return [{
            "requirement": "Banks shall report monthly.",
            "classification": "required",
            "actor": "", "action": "", "object": "",
            "conditions": "", "deadline": "", "evidence": "", "applicability": "",
            "source_unit_ids": [unit_id],
        }]

    monkeypatch.setattr(client, "_extract_checklist_batch", fake_extract)
    batch = [_make_block("u1"), _make_block("u2")]
    units_by_id = {"u1": _make_unit("u1"), "u2": _make_unit("u2")}
    gaps = []

    items = client._run_checklist_batch(
        circular_label="C",
        batch=batch,
        units_by_id=units_by_id,
        gaps=gaps,
    )

    assert len(items) == 2
    assert gaps == []
    assert {item["source_unit_ids"][0] for item in items} == {"u1", "u2"}


def test_run_checklist_batch_records_gap_when_single_block_fails(monkeypatch):
    client = AIClient(AIConfig())

    def fake_extract(*, circular_label, blocks, trace_callback=None):
        raise ValueError("still malformed")

    monkeypatch.setattr(client, "_extract_checklist_batch", fake_extract)
    gaps = []

    items = client._run_checklist_batch(
        circular_label="C",
        batch=[_make_block("u1")],
        units_by_id={"u1": _make_unit("u1")},
        gaps=gaps,
    )

    assert items == []
    assert gaps[0]["reason"] == "checklist_extraction_error"
    assert gaps[0]["block_id"] == "u1"


def test_run_checklist_batch_propagates_rate_limit(monkeypatch):
    client = AIClient(AIConfig())

    class _RateLimited(Exception):
        status_code = 429

    def fake_extract(*, circular_label, blocks, trace_callback=None):
        raise _RateLimited("rate limit exceeded")

    monkeypatch.setattr(client, "_extract_checklist_batch", fake_extract)

    with pytest.raises(_RateLimited):
        client._run_checklist_batch(
            circular_label="C",
            batch=[_make_block("u1"), _make_block("u2")],
            units_by_id={"u1": _make_unit("u1"), "u2": _make_unit("u2")},
            gaps=[],
        )
