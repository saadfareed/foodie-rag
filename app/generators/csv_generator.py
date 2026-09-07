"""CSV export: a titled table with a header row and the data beneath it.

Written with the stdlib `csv` module rather than pandas. `csv.writer` handles the quoting and
escaping rules that actually matter for a file someone will open in Excel, and it does it
without pulling a DataFrame into memory purely to call `.to_csv()` on it -- the rows are already
a list of dicts and pandas adds nothing but the import.

Multiple domains are written as consecutive titled blocks separated by a blank line, not merged
into one wide sheet: `orders` and `vendors` have different columns, and a merged frame is mostly
empty cells with a `domain` discriminator nobody asked for.
"""

import csv
import io

from app.generators.tabular import ReportTable, build_tables, table_note

# Excel interprets a leading =, +, -, or @ in a cell as the start of a formula. A value from the
# database that happens to start with one would execute on open, which is CSV injection.
_FORMULA_PREFIXES = ("=", "+", "-", "@")


def _escape_cell(value: object) -> object:
    """Neutralize a value Excel would otherwise treat as a formula.

    Prefixing with an apostrophe is the conventional fix: Excel shows the original text and
    treats it as a literal string. Numbers are left alone -- they are typed as numeric, not
    parsed as text, so a negative number is never a formula.
    """
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def _write_table(writer: "csv._writer", table: ReportTable) -> None:
    writer.writerow([table.title])
    note = table_note(table)
    if note:
        writer.writerow([note])
    writer.writerow(table.headers)
    for row in table.rows:
        writer.writerow([_escape_cell(cell) for cell in row])


def generate_csv(rows_by_domain: dict[str, list[dict]], title: str | None = None) -> bytes:
    """Render every non-empty domain as a titled CSV block. UTF-8 with a BOM."""
    tables = build_tables(rows_by_domain)

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")

    if title:
        writer.writerow([title])
        writer.writerow([])

    if not tables:
        writer.writerow(["No data matched this question."])
    for index, table in enumerate(tables):
        if index:
            writer.writerow([])
        _write_table(writer, table)

    # utf-8-sig: without the BOM, Excel on Windows opens a UTF-8 CSV as the local codepage and
    # mangles every non-ASCII name in it.
    return buffer.getvalue().encode("utf-8-sig")
