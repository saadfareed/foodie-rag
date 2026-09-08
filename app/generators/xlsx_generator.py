"""XLSX export: one styled worksheet per domain -- title, header row, data table.

This uses openpyxl's normal (not write-only) mode, because the header styling and freeze pane
need addressable cells, which write-only mode does not provide. That costs an in-memory cell
object per cell, which is why REPORT_MAX_ROWS exists to bound it.

The styling is the minimum that makes a sheet usable rather than decorative: a title, a header
that stays visible while scrolling (freeze panes), a filter on the header row, and column widths
wide enough to read. Numbers and dates are written as native types so Excel sorts and formats
them correctly instead of treating them as text.
"""

import io
import re
from datetime import date, datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.dimensions import ColumnDimension

from app.generators.tabular import ReportTable, build_tables, table_note

_HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
_HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
_TITLE_FONT = Font(bold=True, size=14, color="1F4E79")
_NOTE_FONT = Font(italic=True, size=9, color="808080")

_MIN_COLUMN_WIDTH = 10
_MAX_COLUMN_WIDTH = 45

# Excel forbids these in a sheet name, and caps the name at 31 characters.
_INVALID_SHEET_CHARS = re.compile(r"[\\/*?:\[\]]")

_FORMULA_PREFIXES = ("=", "+", "-", "@")


def _safe_sheet_title(name: str, used: set[str]) -> str:
    cleaned = _INVALID_SHEET_CHARS.sub("-", name).strip() or "Sheet"
    cleaned = cleaned[:31]
    candidate, suffix = cleaned, 2
    while candidate.lower() in used:
        # Truncate rather than overflow the 31-char cap when disambiguating.
        candidate = f"{cleaned[: 31 - len(str(suffix)) - 1]}-{suffix}"
        suffix += 1
    used.add(candidate.lower())
    return candidate


def _cell_value(value: object) -> object:
    """Native types survive; anything else is stringified and de-fanged.

    The formula-prefix guard is the same CSV-injection concern as in the CSV writer: a stored
    string beginning with `=` would be evaluated as a formula when the sheet is opened.
    """
    if value is None or isinstance(value, (int, float, bool, datetime, date)):
        return value
    text = str(value)
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def _column_widths(table: ReportTable) -> list[int]:
    """Width per column from the longest rendered value, clamped to a readable range."""
    widths = [len(header) for header in table.headers]
    for row in table.rows:
        for index, cell in enumerate(row):
            if index < len(widths):
                widths[index] = max(widths[index], len(str(cell)) if cell is not None else 0)
    return [min(max(w + 3, _MIN_COLUMN_WIDTH), _MAX_COLUMN_WIDTH) for w in widths]


def _write_sheet(workbook: Workbook, table: ReportTable, used_titles: set[str]) -> None:
    sheet = workbook.create_sheet(_safe_sheet_title(table.title, used_titles))

    # Widths must be set before any row is streamed in write-only mode.
    for index, width in enumerate(_column_widths(table), start=1):
        letter = get_column_letter(index)
        sheet.column_dimensions[letter] = ColumnDimension(sheet, index=letter, width=width)

    # Row positions are computed explicitly rather than derived from max_row + append(). An
    # append([]) does not advance max_row, so the "blank spacer row" that layout implied never
    # actually existed and the header ended up jammed against the title.
    sheet.cell(row=1, column=1, value=table.title).font = _TITLE_FONT

    note = table_note(table)
    if note:
        sheet.cell(row=2, column=1, value=note).font = _NOTE_FONT

    header_row_index = 4 if note else 3  # row 2/3 is the deliberate spacer
    for index, header in enumerate(table.headers, start=1):
        cell = sheet.cell(row=header_row_index, column=index, value=header)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for row_offset, row in enumerate(table.rows, start=1):
        for column_index, value in enumerate(row, start=1):
            sheet.cell(
                row=header_row_index + row_offset, column=column_index, value=_cell_value(value)
            )

    last_column = get_column_letter(max(1, len(table.headers)))
    # Freeze everything above the first data row so headers stay put while scrolling.
    sheet.freeze_panes = sheet.cell(row=header_row_index + 1, column=1)
    sheet.auto_filter.ref = f"A{header_row_index}:{last_column}{sheet.max_row}"


def generate_xlsx(rows_by_domain: dict[str, list[dict]], title: str | None = None) -> bytes:
    """Render every non-empty domain as its own styled worksheet."""
    tables = build_tables(rows_by_domain)

    workbook = Workbook()
    # Workbook() ships with one default sheet; the per-domain sheets are created explicitly.
    workbook.remove(workbook.active)

    if not tables:
        sheet = workbook.create_sheet("Report")
        sheet.cell(row=1, column=1, value=title or "Report").font = _TITLE_FONT
        sheet.cell(row=3, column=1, value="No data matched this question.")
    else:
        used_titles: set[str] = set()
        for table in tables:
            _write_sheet(workbook, table, used_titles)

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
