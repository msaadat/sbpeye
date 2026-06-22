from __future__ import annotations

import hashlib
import html
import re
import threading
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from .documents import document_from_attachment, document_from_circular


# Retained for the existing pdfplumber search extraction path. Checklist parsing
# now reads the original cached PDF with Docling and does not use these markers.
PAGE_MARKER_RE = re.compile(r"(?m)^\[\[SBPEYE_PAGE:(\d+)\]\]\s*$")
DIRECTIVE_RE = re.compile(
    r"\b(shall|must|may|should|required|prohibited|directed|requested|ensure)\b",
    re.IGNORECASE,
)
MAX_UNIT_WORDS = 600
UNIT_PART_WORDS = 450
MAX_BLOCK_WORDS = 1_800
LIST_INDENT_TOLERANCE = 12.0
_CONVERSION_LOCK = threading.Lock()


@dataclass(frozen=True)
class ReferenceUnit:
    unit_id: str
    ref: str
    doc_id: str
    doc_type: str
    doc_label: str
    source_text: str
    heading_path: list[str]
    page_start: int | None
    page_end: int | None
    start_offset: int
    end_offset: int
    oversized: bool
    kind: str = "text"

    def payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AnalysisBlock:
    block_id: str
    ref: str
    doc_id: str
    doc_type: str
    doc_label: str
    block_type: str
    source_text: str
    source_unit_ids: list[str]
    heading_path: list[str]
    page_start: int | None
    page_end: int | None

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def automatic_na_reason(source_text: str) -> str | None:
    """Identify extraction artifacts that do not need an LLM classification."""
    text = re.sub(r"\s+", " ", source_text).strip()
    if re.search(r"<\s*MM\s*-\s*YYYY\s*>|_{5,}|\bxx\s+of\s+20\d{2}\b", text, re.I):
        return "template_placeholder"

    format_count = len(re.findall(r"\bformat\b", text, re.I))
    table_terms = sum(
        bool(re.search(pattern, text, re.I))
        for pattern in (
            r"\bgeneral\s+format\b",
            r"\baccounting\s+format\b",
            r"\bISO\s+3166-1\b",
        )
    )
    if (
        not re.search(r"[.!?;:]", text)
        and not DIRECTIVE_RE.search(text)
        and (format_count >= 3 or table_terms >= 2)
    ):
        return "table_header"
    return None


def _unit_id(doc_id: str, source_ref: str, text: str) -> str:
    digest = hashlib.sha256(
        f"{doc_id}\0{source_ref}\0{text}".encode("utf-8")
    ).hexdigest()[:20]
    return f"{doc_id}:{digest}"


@lru_cache(maxsize=1)
def _document_converter():
    """Create Docling lazily so normal API startup does not initialize its models."""
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    pdf_options = PdfPipelineOptions()
    pdf_options.do_ocr = True
    pdf_options.do_table_structure = True
    return DocumentConverter(
        allowed_formats=[InputFormat.PDF, InputFormat.MD],
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_options),
        },
    )


def _convert_document(document: dict[str, Any]):
    from docling.datamodel.base_models import InputFormat

    local_path = document.get("local_path")
    with _CONVERSION_LOCK:
        converter = _document_converter()
        if local_path:
            path = Path(local_path)
            if not path.is_file():
                raise FileNotFoundError(f"Cached document is missing: {path}")
            return converter.convert(path).document

        text = PAGE_MARKER_RE.sub("", str(document.get("text") or "")).strip()
        if not text:
            raise ValueError("Document has no content to parse.")
        return converter.convert_string(
            html.escape(text),
            format=InputFormat.MD,
            name=str(document.get("doc_label") or "document.md"),
        ).document


def _clean_marker(marker: str | None) -> str:
    value = (marker or "").strip()
    if value.startswith("(") and value.endswith(")"):
        return value
    return value.rstrip(".):")


def _expand_marker(marker: str) -> list[str]:
    """Expand flattened legal markers when Docling cannot retain list nesting."""
    if re.fullmatch(r"\d+(?:\.\d+)+", marker):
        return marker.split(".")
    match = re.fullmatch(r"(\d+(?:\.\d+)*)(\([A-Za-z0-9ivxlcdmIVXLCDM]+\))+$", marker)
    if match:
        return [*match.group(1).split("."), *re.findall(r"\([^)]+\)", marker)]
    return [marker]


def _list_markers(item, dl_doc) -> list[str]:
    from docling_core.types.doc.document import ListItem

    marker_groups: list[list[str]] = []
    current = item
    seen: set[str] = set()
    while current is not None and current.self_ref not in seen:
        seen.add(current.self_ref)
        if isinstance(current, ListItem):
            marker = _clean_marker(current.marker)
            if not marker and current.enumerated and current.parent is not None:
                parent = current.parent.resolve(dl_doc)
                siblings = [
                    ref.resolve(dl_doc)
                    for ref in getattr(parent, "children", [])
                ]
                ordinal = next(
                    (
                        index
                        for index, sibling in enumerate(siblings, start=1)
                        if sibling.self_ref == current.self_ref
                    ),
                    None,
                )
                marker = str(ordinal) if ordinal is not None else ""
            if marker and marker != "-":
                marker_groups.append(_expand_marker(marker))
        parent_ref = getattr(current, "parent", None)
        current = parent_ref.resolve(dl_doc) if parent_ref is not None else None

    markers: list[str] = []
    for group in reversed(marker_groups):
        if len(group) > 1 and markers and group[:len(markers)] == markers:
            markers.extend(group[len(markers):])
        else:
            markers.extend(group)
    return markers


def _item_label(item) -> str:
    return str(getattr(getattr(item, "label", None), "value", getattr(item, "label", "")))


def _item_left(item) -> float | None:
    provenance = list(getattr(item, "prov", None) or [])
    left_edges = [float(entry.bbox.l) for entry in provenance if entry.bbox is not None]
    return min(left_edges) if left_edges else None


def _item_page(item) -> int | None:
    provenance = list(getattr(item, "prov", None) or [])
    return min((entry.page_no for entry in provenance), default=None)


def _item_top(item) -> float | None:
    provenance = list(getattr(item, "prov", None) or [])
    top_edges = [float(entry.bbox.t) for entry in provenance if entry.bbox is not None]
    return max(top_edges) if top_edges else None


def _descendant_doc_items(group, dl_doc) -> list[Any]:
    from docling_core.types.doc.document import DocItem

    descendants: list[Any] = []
    pending = list(getattr(group, "children", None) or [])
    seen: set[str] = set()
    while pending:
        child_ref = pending.pop(0)
        child = child_ref.resolve(dl_doc)
        if child.self_ref in seen:
            continue
        seen.add(child.self_ref)
        if isinstance(child, DocItem):
            descendants.append(child)
        pending[0:0] = list(getattr(child, "children", None) or [])
    return descendants


def _is_document_part_heading(text: str) -> bool:
    return bool(re.match(
        r"^(?:appendix|annex(?:ure)?|schedule|part)\b",
        text.strip(),
        flags=re.IGNORECASE,
    ))


def _pdf_page_part_labels(document: dict[str, Any]) -> dict[int, str]:
    local_path = document.get("local_path")
    if not local_path or Path(local_path).suffix.lower() != ".pdf":
        return {}
    pattern = re.compile(
        r"^(appendix|annex(?:ure)?|schedule|part)\s+[A-Z0-9IVXLCDM.-]+(?:\s+to\b.*)?$",
        re.IGNORECASE,
    )
    try:
        import pdfplumber

        labels: dict[int, str] = {}
        with pdfplumber.open(local_path) as pdf:
            for page_number, page in enumerate(pdf.pages, start=1):
                lines = [
                    re.sub(r"\s+", " ", line).strip()
                    for line in (page.extract_text() or "").splitlines()[:12]
                ]
                label = next((line for line in lines if pattern.match(line)), None)
                if label:
                    labels[page_number] = label
        return labels
    except Exception:
        return {}


def _is_presentation_heading(text: str) -> bool:
    value = re.sub(r"\s+", " ", text).strip()
    if re.match(r"^\[?on\s+(?:the\s+)?letter\s*head\b", value, re.IGNORECASE):
        return True
    if re.match(r"^(?:dear\s+(?:sir|madam)|regards|yours\s+(?:truly|faithfully))\b", value, re.IGNORECASE):
        return True
    return False


def _heading_level(item, text: str) -> int:
    from docling_core.types.doc.document import TitleItem

    if isinstance(item, TitleItem) or _is_document_part_heading(text):
        return 0
    if re.match(r"^(?:\d+(?:\.\d+)*|[A-Za-z]|[ivxlcdm]+)[.)]\s+", text, re.IGNORECASE):
        return 1
    return max(1, int(getattr(item, "level", 1) or 1))


def _layout_list_markers(
    item,
    structural_markers: list[str],
    stack: list[tuple[float, str]],
) -> list[str]:
    """Recover list ancestry from indentation when Docling emits a flat ListGroup."""
    if not structural_markers:
        return []
    if len(structural_markers) > 1:
        stack.clear()
        return structural_markers

    marker = structural_markers[0]
    left = _item_left(item)
    if left is None:
        return structural_markers
    while stack and left < stack[-1][0] - LIST_INDENT_TOLERANCE:
        stack.pop()
    if not stack:
        stack.append((left, marker))
    elif left > stack[-1][0] + LIST_INDENT_TOLERANCE:
        stack.append((left, marker))
    else:
        stack[-1] = (left, marker)
    return [entry[1] for entry in stack]


def _heading_references(heading_path: list[str]) -> list[str]:
    references: list[str] = []
    structural_index = 0
    for heading in heading_path:
        if _is_document_part_heading(heading):
            references.append(heading)
            continue
        label = "Section" if structural_index == 0 else "Subsection"
        structural_index += 1
        references.append(
            heading if heading.casefold().startswith(label.casefold()) else f"{label} {heading}"
        )
    return references


def _hierarchical_reference(
    heading_path: list[str],
    *,
    markers: list[str] | None = None,
    terminal: str | None = None,
) -> str:
    parts = _heading_references(heading_path)
    marker_labels = ("Para", "Sub-para", "Clause", "Sub-clause", "Item")
    for index, marker in enumerate(markers or []):
        label = marker_labels[index] if index < len(marker_labels) else "Sub-item"
        parts.append(f"{label} {marker}")
    if terminal:
        parts.append(terminal)
    return " > ".join(parts)


def _page_range(item, descendants: list[Any] | None = None) -> tuple[int | None, int | None, int, int]:
    provenance = list(getattr(item, "prov", None) or [])
    for descendant in descendants or []:
        provenance.extend(list(getattr(descendant, "prov", None) or []))
    pages = [entry.page_no for entry in provenance]
    spans = [entry.charspan for entry in provenance if entry.charspan]
    return (
        min(pages) if pages else None,
        max(pages) if pages else None,
        min((span[0] for span in spans), default=0),
        max((span[1] for span in spans), default=0),
    )


def _serialized_item_text(item, serializer) -> str:
    from docling_core.types.doc.document import ListItem, TableItem

    if isinstance(item, TableItem):
        return serializer.serialize(item=item).text.strip()
    text = str(getattr(item, "text", "") or "").strip()
    if isinstance(item, ListItem):
        marker = str(item.marker or "").strip()
        if marker and marker != "-" and not text.startswith(marker):
            text = f"{marker} {text}"
    return text


def _split_oversized_unit(unit: ReferenceUnit) -> list[ReferenceUnit]:
    words = unit.source_text.split()
    if len(words) <= MAX_UNIT_WORDS or unit.kind in {"form", "table"}:
        return [unit]
    parts: list[ReferenceUnit] = []
    for index, start in enumerate(range(0, len(words), UNIT_PART_WORDS), start=1):
        text = " ".join(words[start:start + UNIT_PART_WORDS])
        ref = f"{unit.ref} (part {index})"
        parts.append(replace(
            unit,
            unit_id=_unit_id(unit.doc_id, f"{unit.unit_id}:{index}", text),
            ref=ref,
            source_text=text,
            oversized=True,
        ))
    return parts


def reference_units_from_docling(
    document: dict[str, Any], dl_doc
) -> list[ReferenceUnit]:
    """Adapt Docling document items into SBPEye's stable checklist contract."""
    from docling_core.transforms.serializer.markdown import MarkdownDocSerializer
    from docling_core.types.doc.document import (
        DocItem,
        GroupItem,
        ListItem,
        SectionHeaderItem,
        TableItem,
        TitleItem,
    )

    serializer = MarkdownDocSerializer(doc=dl_doc)
    page_part_labels = _pdf_page_part_labels(document)
    headings: dict[int, str] = {}
    paragraph_counts: dict[tuple[str, ...], int] = {}
    table_count = 0
    units: list[ReferenceUnit] = []
    list_stack: list[tuple[float, str]] = []
    reference_counts: dict[str, int] = {}
    excluded_labels = {"page_header", "page_footer", "footnote", "caption"}

    form_groups = [
        item for item, _ in dl_doc.iterate_items(with_groups=True)
        if isinstance(item, GroupItem) and _item_label(item) == "form_area"
    ]
    form_descendants = {
        descendant.self_ref
        for group in form_groups
        for descendant in _descendant_doc_items(group, dl_doc)
    }
    applied_headings: set[str] = set()
    page_headings: dict[int, list[Any]] = {}
    for candidate, _ in dl_doc.iterate_items(with_groups=True):
        if (
            isinstance(candidate, TitleItem | SectionHeaderItem)
            and candidate.self_ref not in form_descendants
            and not _is_presentation_heading(candidate.text)
            and _item_page(candidate) is not None
        ):
            page_headings.setdefault(_item_page(candidate), []).append(candidate)
    for candidates in page_headings.values():
        candidates.sort(key=lambda value: _item_top(value) or 0, reverse=True)

    def apply_heading(candidate) -> None:
        nonlocal headings
        heading_text = candidate.text.strip()
        if (
            candidate.self_ref in applied_headings
            or not heading_text
            or _is_presentation_heading(heading_text)
        ):
            return
        if (
            len(heading_text.split()) <= 2
            and heading_text.endswith(".")
            and re.search(
                r"\b(?:application|certificate|proforma|questionnaire|template|form)\b",
                " ".join(headings.values()),
                re.IGNORECASE,
            )
        ):
            applied_headings.add(candidate.self_ref)
            return
        level = _heading_level(candidate, heading_text)
        headings = {key: value for key, value in headings.items() if key < level}
        headings[level] = heading_text
        list_stack.clear()
        applied_headings.add(candidate.self_ref)

    def ensure_page_part(page_number: int | None) -> None:
        nonlocal headings
        page_part = page_part_labels.get(page_number) if page_number is not None else None
        if page_part and headings.get(0) != page_part:
            headings = {0: page_part}
            list_stack.clear()

    def unique_reference(ref: str) -> str:
        reference_counts[ref] = reference_counts.get(ref, 0) + 1
        occurrence = reference_counts[ref]
        return ref if occurrence == 1 else f"{ref} (occurrence {occurrence})"

    for item, _ in dl_doc.iterate_items(with_groups=True):
        if isinstance(item, GroupItem) and _item_label(item) == "form_area":
            descendants = _descendant_doc_items(item, dl_doc)
            descendant_pages = [_item_page(child) for child in descendants if _item_page(child) is not None]
            ensure_page_part(min(descendant_pages) if descendant_pages else None)
            source_text = serializer.serialize(item=item).text.strip()
            if not source_text:
                continue
            source_text = re.sub(r"[ \t]+", " ", source_text)
            form_heading = next(
                (
                    child.text.strip()
                    for child in descendants
                    if isinstance(child, TitleItem | SectionHeaderItem)
                    and child.text.strip()
                    and not _is_presentation_heading(child.text)
                    and not re.match(r"^it\s+is\s+(?:hereby\s+)?certified\b", child.text, re.IGNORECASE)
                ),
                None,
            )
            form_headings = [headings[key] for key in sorted(headings)]
            if form_heading and (not form_headings or form_headings[-1] != form_heading):
                if form_headings and not _is_document_part_heading(form_headings[-1]):
                    form_headings[-1] = form_heading
                else:
                    form_headings.append(form_heading)
            ref = unique_reference(_hierarchical_reference(form_headings, terminal="Form"))
            page_start, page_end, start_offset, end_offset = _page_range(item, descendants)
            unit = ReferenceUnit(
                unit_id=_unit_id(document["doc_id"], item.self_ref, source_text),
                ref=ref,
                doc_id=document["doc_id"],
                doc_type=document["doc_type"],
                doc_label=document["doc_label"],
                source_text=source_text,
                heading_path=form_headings,
                page_start=page_start,
                page_end=page_end,
                start_offset=start_offset,
                end_offset=end_offset,
                oversized=len(source_text.split()) > MAX_UNIT_WORDS,
                kind="form",
            )
            units.append(unit)
            continue
        if getattr(item, "self_ref", None) in form_descendants:
            continue
        if isinstance(item, TitleItem | SectionHeaderItem):
            ensure_page_part(_item_page(item))
            apply_heading(item)
            continue
        if not isinstance(item, DocItem):
            continue
        page_no = _item_page(item)
        ensure_page_part(page_no)
        item_top = _item_top(item)
        if page_no is not None and item_top is not None:
            for candidate in page_headings.get(page_no, []):
                candidate_top = _item_top(candidate)
                if candidate_top is not None and candidate_top > item_top + 2:
                    apply_heading(candidate)
        if _item_label(item) in excluded_labels:
            continue
        raw_item_text = str(getattr(item, "text", "") or "").strip()
        if raw_item_text and _is_document_part_heading(raw_item_text):
            if headings.get(0) != raw_item_text:
                headings = {0: raw_item_text}
                list_stack.clear()
            continue

        source_text = _serialized_item_text(item, serializer)
        if not source_text or not re.search(r"[A-Za-z0-9]", source_text):
            continue

        heading_path = [headings[key] for key in sorted(headings)]
        markers = (
            _layout_list_markers(item, _list_markers(item, dl_doc), list_stack)
            if isinstance(item, ListItem)
            else []
        )
        ref = _hierarchical_reference(heading_path, markers=markers) if markers else None
        kind = "text"
        if not ref:
            if isinstance(item, TableItem):
                table_count += 1
                ref = _hierarchical_reference(
                    heading_path, terminal=f"Table {table_count}"
                )
                kind = "table"
            else:
                heading_key = tuple(heading_path)
                paragraph_counts[heading_key] = paragraph_counts.get(heading_key, 0) + 1
                para = f"Para {paragraph_counts[heading_key]}"
                ref = _hierarchical_reference(heading_path, terminal=para)

        ref = unique_reference(ref)
        page_start, page_end, start_offset, end_offset = _page_range(item)
        source_ref = str(getattr(item, "self_ref", ref))
        unit = ReferenceUnit(
            unit_id=_unit_id(document["doc_id"], source_ref, source_text),
            ref=ref,
            doc_id=document["doc_id"],
            doc_type=document["doc_type"],
            doc_label=document["doc_label"],
            source_text=source_text,
            heading_path=heading_path,
            page_start=page_start,
            page_end=page_end,
            start_offset=start_offset,
            end_offset=end_offset,
            oversized=len(source_text.split()) > MAX_UNIT_WORDS,
            kind=kind,
        )
        units.extend(_split_oversized_unit(unit))
    return units


def segment_document(document: dict[str, Any]) -> list[ReferenceUnit]:
    return reference_units_from_docling(document, _convert_document(document))


def build_checklist_corpus(circular) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    documents: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    circular_document = document_from_circular(circular)
    if circular_document["text"].strip():
        documents.append(circular_document)
    else:
        gaps.append({
            "doc_id": circular.id,
            "doc_type": "circular",
            "doc_label": circular_document["doc_label"],
            "reason": "missing_text",
        })

    for attachment in sorted(circular.attachments, key=lambda item: item.filename.lower()):
        if (attachment.file_type or "").lower() != "pdf":
            continue
        document = document_from_attachment(attachment)
        local_path = document.get("local_path")
        if local_path and Path(local_path).is_file():
            documents.append(document)
        else:
            gaps.append({
                "doc_id": attachment.id,
                "doc_type": "attachment",
                "doc_label": attachment.filename,
                "reason": "missing_file",
                "error": attachment.extraction_error,
            })
    return documents, gaps


def segment_corpus(documents: list[dict[str, Any]]) -> list[ReferenceUnit]:
    return [unit for document in documents for unit in segment_document(document)]


def _analysis_block_id(doc_id: str, unit_ids: list[str]) -> str:
    digest = hashlib.sha256(f"{doc_id}\0{'|'.join(unit_ids)}".encode("utf-8")).hexdigest()[:20]
    return f"{doc_id}:block:{digest}"


def _block_source_text(units: list[ReferenceUnit]) -> str:
    sections: list[str] = []
    for unit in units:
        page = unit.page_start if unit.page_start is not None else "HTML"
        sections.append(
            f"[SOURCE_ID: {unit.unit_id}]\n"
            f"Reference: {unit.ref}\n"
            f"Page: {page}\n"
            f"{unit.source_text}"
        )
    return "\n\n".join(sections)


def build_analysis_blocks(units: list[ReferenceUnit]) -> list[AnalysisBlock]:
    """Group provenance-rich atoms into complete sections for checklist extraction."""
    grouped: list[list[ReferenceUnit]] = []
    current: list[ReferenceUnit] = []
    current_key: tuple[Any, ...] | None = None
    current_words = 0

    def flush() -> None:
        nonlocal current, current_key, current_words
        if current:
            if (
                any(unit.kind in {"form", "table"} for unit in current)
                or not all(automatic_na_reason(unit.source_text) for unit in current)
            ):
                grouped.append(current)
        current = []
        current_key = None
        current_words = 0

    for unit in units:
        word_count = len(unit.source_text.split())
        atomic = unit.kind in {"form", "table"}
        key = (
            unit.doc_id,
            tuple(unit.heading_path),
            unit.kind if atomic else "section",
            unit.page_start if not unit.heading_path else None,
        )
        if atomic:
            flush()
            current = [unit]
            flush()
            continue
        if current and (key != current_key or current_words + word_count > MAX_BLOCK_WORDS):
            flush()
        if not current:
            current_key = key
        current.append(unit)
        current_words += word_count
    flush()

    def group_type(block_units: list[ReferenceUnit]) -> str:
        if any(unit.kind == "form" for unit in block_units):
            return "form"
        heading_text = " ".join(block_units[0].heading_path)
        combined_text = " ".join(unit.source_text for unit in block_units)
        form_heading = re.search(
            r"\b(?:application|certificate|proforma|questionnaire|template|form)\b",
            heading_text,
            re.IGNORECASE,
        )
        placeholders = len(re.findall(
            r"_{3,}|(?:\\_){3,}|<[^>]+>|\b20xx\b",
            combined_text,
            re.IGNORECASE,
        ))
        if form_heading or placeholders >= 2:
            return "form"
        if any(unit.kind == "table" for unit in block_units):
            return "table"
        return "regulation"

    merged_groups: list[list[ReferenceUnit]] = []
    for block_units in grouped:
        block_kind = group_type(block_units)
        part = next(
            (heading for heading in block_units[0].heading_path if _is_document_part_heading(heading)),
            None,
        )
        if merged_groups:
            previous = merged_groups[-1]
            previous_kind = group_type(previous)
            previous_part = next(
                (heading for heading in previous[0].heading_path if _is_document_part_heading(heading)),
                None,
            )
            combined_words = sum(len(unit.source_text.split()) for unit in [*previous, *block_units])
            previous_words = sum(len(unit.source_text.split()) for unit in previous)
            compatible_kinds = (
                block_kind in {"form", "table"}
                and (
                    previous_kind in {"form", "table"}
                    or (previous_kind == "regulation" and previous_words <= 60)
                )
            )
            if (
                part is not None
                and part == previous_part
                and compatible_kinds
                and combined_words <= MAX_BLOCK_WORDS
            ):
                previous.extend(block_units)
                continue
        merged_groups.append(block_units)
    grouped = merged_groups

    blocks: list[AnalysisBlock] = []
    ref_counts: dict[str, int] = {}
    for block_units in grouped:
        first = block_units[0]
        heading_path = list(first.heading_path)
        block_type = group_type(block_units)
        base_ref = _hierarchical_reference(heading_path)
        if not base_ref:
            page = first.page_start if first.page_start is not None else "HTML"
            base_ref = f"Page {page}"
        if len(block_units) == 1 and first.kind in {"form", "table"}:
            base_ref = first.ref
        elif block_type in {"form", "table"}:
            base_ref = _hierarchical_reference(heading_path, terminal=block_type.title())
        ref_counts[base_ref] = ref_counts.get(base_ref, 0) + 1
        occurrence = ref_counts[base_ref]
        ref = base_ref if occurrence == 1 else f"{base_ref} (block {occurrence})"
        unit_ids = [unit.unit_id for unit in block_units]
        pages = [
            page
            for unit in block_units
            for page in (unit.page_start, unit.page_end)
            if page is not None
        ]
        blocks.append(AnalysisBlock(
            block_id=_analysis_block_id(first.doc_id, unit_ids),
            ref=ref,
            doc_id=first.doc_id,
            doc_type=first.doc_type,
            doc_label=first.doc_label,
            block_type=block_type,
            source_text=_block_source_text(block_units),
            source_unit_ids=unit_ids,
            heading_path=heading_path,
            page_start=min(pages) if pages else None,
            page_end=max(pages) if pages else None,
        ))
    return blocks


def prepare_reference_chunks(
    document: dict[str, Any], max_words: int = 350, overlap_words: int = 75
) -> list[dict[str, Any]]:
    """Prepare lightweight retrieval chunks without invoking the PDF pipeline."""
    chunks: list[dict[str, Any]] = []
    text = str(document.get("text") or "").strip()
    page_matches = list(PAGE_MARKER_RE.finditer(text))
    pages: list[tuple[int | None, str]] = []
    if page_matches:
        for index, match in enumerate(page_matches):
            end = page_matches[index + 1].start() if index + 1 < len(page_matches) else len(text)
            pages.append((int(match.group(1)), text[match.end():end].strip()))
    elif text:
        pages.append((None, text))

    chunk_index = 0
    source_cursor = 0
    for page_number, page_text in pages:
        words = page_text.split()
        step = max(1, max_words - overlap_words)
        starts = [0] if len(words) <= max_words else list(range(0, len(words), step))
        for page_chunk_index, start in enumerate(starts):
            body = " ".join(words[start:start + max_words])
            if not body:
                continue
            chunk_index += 1
            ref = f"Page {page_number}" if page_number is not None else f"Chunk {chunk_index}"
            unit_id = _unit_id(document["doc_id"], f"{page_number}:{page_chunk_index}", body)
            chunks.append({
                "text": f"{document['doc_label']}. {ref}. {body}",
                "ref": ref,
                "unit_id": unit_id,
                "page_start": page_number,
                "page_end": page_number,
                "source_start": source_cursor,
                "source_end": source_cursor + len(body),
                "unit_chunk_index": page_chunk_index,
            })
            source_cursor += len(body)
            if start + max_words >= len(words):
                break
    return chunks


def compact_required_checklist(value: str | dict[str, Any] | None) -> list[dict[str, Any]]:
    if not value:
        return []
    if isinstance(value, str):
        try:
            import json

            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    if not isinstance(value, dict):
        return []

    checklist_items = value.get("checklist_items")
    if isinstance(checklist_items, list):
        return [
            {
                "ref": item.get("ref"),
                "doc_label": item.get("doc_label"),
                "requirement": item.get("requirement"),
            }
            for item in checklist_items
            if isinstance(item, dict) and item.get("classification") == "required"
        ]

    return [
        {
            "ref": unit.get("ref"),
            "doc_label": unit.get("doc_label"),
            "requirement": unit.get("source_text"),
        }
        for unit in value.get("source_units", [])
        if isinstance(unit, dict) and unit.get("classification") == "required"
    ]
