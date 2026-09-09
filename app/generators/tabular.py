"""Turn `rows_by_domain` into bounded, presentation-ready tables.

Every export format (CSV, XLSX, PDF) needs the same three things before it can render anything:
a stable column order, a cap on how much is emitted, and values flattened into something a cell
can hold. Doing that once here -- rather than three times, slightly differently -- is what keeps
a CSV, an XLSX and a PDF of the same question showing the same columns in the same order.

Rows arriving here have already been through app/security/field_policy.py in the executor, so
this module is purely about presentation. It deliberately does no filtering of its own: a second,
half-remembered copy of the field policy is exactly how the two drift apart.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from math import isfinite
from typing import Any

from app.agents.domains import report_columns_for, report_hidden_columns_for
from app.config import settings

# Headers where the mechanical `snake_case -> Title Case` rendering reads wrong or says less
# than it could. `order_id` becoming "Order ID" is fine in isolation but "Order #" is what a
# person calls it; `amount` alone doesn't say what kind of amount; `status` on an order row is
# specifically its current state.
_HEADER_OVERRIDES = {
    "order_id": "Order #",
    "amount": "Order Payment",
    "status": "Current Status",
    "order_type": "Order Type",
    "customer_name": "Customer Name",
    "vendor_name": "Vendor Name",
    "business_name": "Vendor Name",
    "payment_method": "Payment Method",
    "created_at": "Created At",
    "user_id": "User ID",
    "loyalty_tier": "Loyalty Tier",
    "last_active_at": "Last Active At",
}


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
    """`vendor_id` -> `Vendor ID`. Column headers are read by people, not by the query planner.

    Checks _HEADER_OVERRIDES first for the columns whose natural English name isn't just their
    field name title-cased.
    """
    override = _HEADER_OVERRIDES.get(name)
    if override:
        return override
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
    if isinstance(value, float) and not isfinite(value):
        # inf/nan reach here from aggregations that divided by zero. Excel has no
        # representation for them, and openpyxl writes a *silently blank cell* -- so the CSV
        # said "inf" while the spreadsheet said nothing at all. Rendering them as text keeps all
        # three formats agreeing and keeps a real value from vanishing.
        return str(value)
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


def _ordered_columns(rows: list[dict], preferred: list[str] | None = None) -> list[str]:
    """Union of keys across rows: `preferred` ones first in that order, then first-seen order.

    Scanning every row (not just the first) matters because Mongo documents are not uniform --
    an optional field present only on row 40 would otherwise be silently dropped from the table.

    `preferred` is the domain's declared reading order (`DomainConfig.report_columns`). Without
    it the fallback is first-seen, which follows Mongo's key order -- an implementation detail
    that puts `customer_id` before `customer_name` and buries `status` in the middle. Preferred
    columns absent from the rows are skipped rather than emitted as empty columns.
    """
    seen: dict[str, None] = {}
    for row in rows:
        for key in row:
            seen.setdefault(key, None)

    if not preferred:
        return list(seen)

    ordered = [column for column in preferred if column in seen]
    ordered.extend(column for column in seen if column not in set(preferred))
    return ordered


def build_table(
    name: str,
    rows: list[dict],
    preferred_columns: list[str] | None = None,
    max_rows: int | None = None,
) -> ReportTable | None:
    """Build one bounded table, or None when there's nothing to show.

    `preferred_columns` defaults to the domain's declared order when `name` is a known domain,
    so callers don't have to look it up.

    `max_rows` overrides the configured cap for this call -- that is how a per-role report limit
    works (`settings.report_max_rows_for`). REPORT_MAX_ROWS remains the absolute ceiling: an
    override can only lower it, never raise it, so a role can't be configured past the bound the
    render pool was sized for. The truncation note is computed from the effective cap, so a
    smaller table still says it was cut.
    """
    if not rows:
        return None

    if preferred_columns is None:
        preferred_columns = report_columns_for(name)

    all_columns = _ordered_columns(rows, preferred_columns)
    hidden = report_hidden_columns_for(name)
    if hidden:
        remaining = [c for c in all_columns if c not in hidden]
        # Only actually hide them if something survives. A pipeline that projected *only*
        # hidden columns (e.g. a question specifically about isWallet) would otherwise render
        # an empty table -- worse than showing the raw column.
        if remaining:
            all_columns = remaining

    if not all_columns:
        return None

    max_columns = max(1, settings.report_max_columns)
    columns = all_columns[:max_columns]
    dropped = all_columns[max_columns:]

    ceiling = max(1, settings.report_max_rows)
    effective_max_rows = ceiling if max_rows is None else max(1, min(max_rows, ceiling))
    visible_rows = rows[:effective_max_rows]

    return ReportTable(
        name=name,
        headers=[humanize_header(c) for c in columns],
        rows=[[flatten_value(row.get(c)) for c in columns] for row in visible_rows],
        total_row_count=len(rows),
        truncated_rows=len(rows) > effective_max_rows,
        dropped_columns=dropped,
    )


def build_tables(
    rows_by_domain: dict[str, list[dict]], max_rows: int | None = None
) -> list[ReportTable]:
    """One table per domain that actually returned rows, in domain order."""
    tables = []
    for domain, rows in rows_by_domain.items():
        table = build_table(domain, rows, max_rows=max_rows)
        if table is not None:
            tables.append(table)
    return tables


_ISO_DATETIME_TEXT = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")


def render_cell(value: Any) -> str:
    """Format a cell for a text-based output (CSV, PDF) -- XLSX keeps native types instead.

    Datetimes are the reason this exists. `str(datetime)` gives
    "2026-09-07 20:08:38.461897+00:00": microseconds nobody asked for and an offset that is
    always UTC here, in the widest column of the table. Minute precision is what a report reader
    actually uses.

    Datetimes arrive here as *strings*, not datetime objects, because `app/db/executor.py`'s
    `_to_jsonable` stringifies them on the way out of Mongo. Both forms are handled: the string
    branch is the one that actually fires today, and the object branch keeps this correct if a
    caller ever passes unconverted rows.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, str) and _ISO_DATETIME_TEXT.match(value):
        try:
            return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return value
    if isinstance(value, float):
        # Trim float noise (0.30000000000000004) without forcing 2dp onto whole numbers.
        return f"{value:.2f}".rstrip("0").rstrip(".") if value % 1 else str(int(value))
    return str(value)


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
