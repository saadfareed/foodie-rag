"""Turn `rows_by_domain` into bounded, presentation-ready tables.

Every export format (CSV, XLSX, PDF) needs the same three things before it can render anything:
a stable column order, a cap on how much is emitted, and values flattened into something a cell
can hold. Doing that once here -- rather than three times, slightly differently -- is what keeps
a CSV, an XLSX and a PDF of the same question showing the same columns in the same order.

Rows arriving here have already been through app/security/field_policy.py in the executor, so
this module is purely about presentation. It deliberately does no filtering of its own: a second,
half-remembered copy of the field policy is exactly how the two drift apart.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.config import settings


@dataclass(frozen=True)
class ReportTable:
    """One domain's rows, ready to render in any format."""

    name: str
    headers: list[str]
    rows: list[list[Any]]
    total_row_count: int
    #: True when `rows` is a prefix of a larger result set (see settings.report_max_rows).
    truncated_rows: bool = False
    #: Columns dropped to stay within settings.report_max_columns.
    dropped_columns: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.name.replace("_", " ").title()


def humanize_header(name: str) -> str:
    """`vendor_id` -> `Vendor ID`. Column headers are read by people, not by the query planner."""
    words = [w for w in name.replace(".", " ").replace("_", " ").split() if w]
    out = []
    for word in words:
        if word.lower() in {"id", "ids"}:
            out.append(word.upper())
        elif word.isupper():
            out.append(word)
        else:
            out.append(word.capitalize())
    return " ".join(out) or name


def flatten_value(value: Any) -> Any:
    """Reduce a Mongo value to something a spreadsheet cell can hold.

    Numbers and dates pass through as native types so XLSX keeps them numeric/sortable and the
    chart layer can still do arithmetic on them; everything structural becomes a compact string.
    """
    if value is None or isinstance(value, (str, int, float, bool, datetime, date)):
        return value
    if isinstance(value, dict):
        # GeoJSON is the common case and reads far better as a coordinate pair than as JSON.
        if value.get("type") == "Point" and isinstance(value.get("coordinates"), list):
            coords = value["coordinates"]
            if len(coords) == 2:
                return f"{coords[1]}, {coords[0]}"
        return ", ".join(f"{k}={flatten_value(v)}" for k, v in value.items())
    if isinstance(value, list):
        return ", ".join(str(flatten_value(v)) for v in value)
    return str(value)


def _ordered_columns(rows: list[dict]) -> list[str]:
    """Union of keys across rows, in first-seen order.

    First-seen beats sorted: the query's own projection order usually puts the identifying
    column first, which is where a reader looks. Scanning every row (not just the first) matters
    because Mongo documents are not uniform -- an optional field present only on row 40 would
    otherwise be silently dropped from the table.
    """
    seen: dict[str, None] = {}
    for row in rows:
        for key in row:
            seen.setdefault(key, None)
    return list(seen)


def build_table(name: str, rows: list[dict]) -> ReportTable | None:
    """Build one bounded table, or None when there's nothing to show."""
    if not rows:
        return None

    all_columns = _ordered_columns(rows)
    if not all_columns:
        return None

    max_columns = max(1, settings.report_max_columns)
    columns = all_columns[:max_columns]
    dropped = all_columns[max_columns:]

    max_rows = max(1, settings.report_max_rows)
    visible_rows = rows[:max_rows]

    return ReportTable(
        name=name,
        headers=[humanize_header(c) for c in columns],
        rows=[[flatten_value(row.get(c)) for c in columns] for row in visible_rows],
        total_row_count=len(rows),
        truncated_rows=len(rows) > max_rows,
        dropped_columns=dropped,
    )


def build_tables(rows_by_domain: dict[str, list[dict]]) -> list[ReportTable]:
    """One table per domain that actually returned rows, in domain order."""
    tables = []
    for domain, rows in rows_by_domain.items():
        table = build_table(domain, rows)
        if table is not None:
            tables.append(table)
    return tables


def table_note(table: ReportTable) -> str | None:
    """A one-line caveat when a table is not the whole picture, or None when it is.

    Silently truncating and saying nothing is the failure mode worth avoiding here: a reader who
    is not told will treat 1000 rows as the complete result set.
    """
    notes = []
    if table.truncated_rows:
        notes.append(f"showing first {len(table.rows):,} of {table.total_row_count:,} rows")
    if table.dropped_columns:
        notes.append(f"{len(table.dropped_columns)} further column(s) omitted")
    return "; ".join(notes).capitalize() if notes else None
