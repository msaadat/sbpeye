from __future__ import annotations

from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


HEADER_FILL = PatternFill("solid", fgColor="006B3C")
HEADER_FONT = Font(color="FFFFFF", bold=True)
MAX_CELL_LENGTH = 32_767


def _cell_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (int, float, bool)):
        return value
    text = str(value)[:MAX_CELL_LENGTH]
    if text.startswith(("=", "+", "-", "@")):
        return f"'{text}"
    return text


def _style_table(sheet, widths: list[int]) -> None:
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for cell in sheet[1]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center")
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)


def build_checklist_workbook(circular, checklist: dict[str, Any]) -> BytesIO:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Checklist"
    headers = [
        "Circular Reference", "Circular Title", "Department", "Circular Date",
        "Source Document", "Source Type", "Reference", "Page Start", "Page End",
        "Classification", "Requirement", "Actor", "Applicability", "Deadline",
        "Evidence", "Conditions", "Source Excerpt",
    ]
    sheet.append(headers)

    circular_date = circular.date.strftime("%Y-%m-%d") if circular.date else ""
    items = [
        item for item in checklist.get("checklist_items", [])
        if isinstance(item, dict)
    ]
    if not items:
        items = [
            {
                **unit,
                "requirement": unit.get("source_text"),
            }
            for unit in checklist.get("source_units", [])
            if isinstance(unit, dict)
            and unit.get("classification") in {"required", "optional"}
        ]
    for item in items:
        if not isinstance(item, dict):
            continue
        sheet.append([_cell_value(value) for value in [
            circular.reference, circular.title, circular.department, circular_date,
            item.get("doc_label"), item.get("doc_type"), item.get("ref"),
            item.get("page_start"), item.get("page_end"), item.get("classification"),
            item.get("requirement"), item.get("actor"), item.get("applicability"),
            item.get("deadline"), item.get("evidence"), item.get("conditions"),
            item.get("source_text"),
        ]])
    _style_table(sheet, [20, 38, 16, 14, 28, 14, 38, 11, 11, 14, 60, 24, 28, 22, 38, 38, 60])

    summary = workbook.create_sheet("Summary")
    summary.append(["Field", "Value"])
    units = [unit for unit in checklist.get("source_units", []) if isinstance(unit, dict)]
    blocks = [block for block in checklist.get("analysis_blocks", []) if isinstance(block, dict)]
    summary_rows = [
        ("Circular Reference", circular.reference),
        ("Circular Title", circular.title),
        ("Department", circular.department),
        ("Circular Date", circular_date),
        ("Circular URL", circular.url),
        ("Checklist Status", checklist.get("status")),
        ("Generated At", checklist.get("generated_at")),
        ("Checklist Items", len(items)),
        ("Required", sum(item.get("classification") == "required" for item in items)),
        ("Optional", sum(item.get("classification") == "optional" for item in items)),
        ("Source Units", len(units)),
        ("Analysis Blocks", len(blocks)),
        ("Coverage Gaps", len(checklist.get("coverage_gaps") or [])),
    ]
    for field, value in summary_rows:
        summary.append([field, _cell_value(value)])
    _style_table(summary, [24, 80])

    gaps = workbook.create_sheet("Coverage Gaps")
    gaps.append(["Document", "Document Type", "Reference", "Page Start", "Page End", "Reason", "Error"])
    for gap in checklist.get("coverage_gaps") or []:
        if isinstance(gap, dict):
            gaps.append([_cell_value(gap.get(key)) for key in (
                "doc_label", "doc_type", "ref", "page_start", "page_end", "reason", "error"
            )])
    _style_table(gaps, [35, 18, 38, 11, 11, 24, 60])

    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return output
