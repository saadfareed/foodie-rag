"""Format generators: the shared tabular layer, chart selection, and the three output formats.

The file bytes themselves are checked structurally (magic numbers, parsed back with openpyxl,
decoded CSV text) rather than byte-for-byte -- a golden-file comparison of a PDF would break on
every WeasyPrint patch release without telling us anything about correctness.
"""

import io
from datetime import datetime

import pytest
from openpyxl import load_workbook

from app.generators.charts import choose_chart, extract_series, render_chart_png
from app.generators.csv_generator import generate_csv
from app.generators.pdf_generator import generate_pdf
from app.generators.tabular import (
    build_table,
    build_tables,
    humanize_header,
    render_cell,
    table_note,
)
from app.generators.xlsx_generator import generate_xlsx

_ORDER_ROWS = [
    {
        "order_id": f"ORD-{i}",
        "status": ["pending", "delivered", "cancelled"][i % 3],
        "amount": i * 10.0,
    }
    for i in range(1, 13)
]
_ROWS_BY_DOMAIN = {"orders": _ORDER_ROWS, "vendors": [{"user_id": "USR-1", "rating": 4.5}]}


# --- tabular ---------------------------------------------------------------------------------


def test_humanize_header_uppercases_id_and_titles_the_rest():
    assert humanize_header("vendor_id") == "Vendor ID"
    assert humanize_header("category") == "Category"


def test_humanize_header_uses_the_name_a_person_would_use():
    """The mechanical rendering is right often enough, but "Order ID" isn't what anyone calls
    an order number and a bare "Amount" doesn't say what kind."""
    assert humanize_header("order_id") == "Order #"
    assert humanize_header("amount") == "Order Payment"
    assert humanize_header("status") == "Current Status"
    assert humanize_header("business_name") == "Vendor Name"


def test_build_table_unions_columns_across_non_uniform_rows():
    """Mongo documents aren't uniform -- a field present only on a later row must still appear
    as a column, or it's silently dropped from the export."""
    table = build_table("orders", [{"a": 1}, {"a": 2, "b": 3}])

    assert table.headers == ["A", "B"]
    assert table.rows == [[1, None], [2, 3]]


def test_build_table_returns_none_for_no_rows():
    assert build_table("orders", []) is None


def test_declared_report_columns_lead_in_their_declared_order():
    """Mongo key order is an implementation detail, not a reading order -- a reader wants to see
    who the order is for and which order it is before the payment internals."""
    row = {
        "payment_method": "cash",
        "status": "pending",
        "vendor_name": "Al-Noor Restaurant",
        "amount": 120.5,
        "order_type": "delivery",
        "order_id": "ORD-1",
        "customer_name": "Ayesha Khan",
    }
    table = build_table("orders", [row])

    assert table.headers[:6] == [
        "Customer Name",
        "Order #",
        "Order Payment",
        "Order Type",
        "Current Status",
        "Vendor Name",
    ]


def test_payment_plumbing_is_hidden_from_reports():
    """onlinepaymentmethod/isWallet are the storage encoding payment_method already names in
    words, and payby is its per-method breakdown. Answerable, but not columns a person reads."""
    table = build_table(
        "orders",
        [{"order_id": "ORD-1", "onlinepaymentmethod": 2, "isWallet": True, "payby": {"card": 10}}],
    )

    assert table.headers == ["Order #"]


def test_hidden_columns_are_kept_when_nothing_else_survives():
    """A question specifically about isWallet would otherwise render an empty table."""
    table = build_table("orders", [{"isWallet": True}, {"isWallet": False}])

    assert table.headers == ["Iswallet"]


def test_render_cell_trims_datetime_noise():
    """Executor rows carry datetimes as strings; str(datetime) is microseconds and an offset
    that is always UTC, in the widest column of the table."""
    assert render_cell("2026-09-07 20:11:40.640857+00:00") == "2026-09-07 20:11"
    assert render_cell("2026-09-07T20:11:40Z") == "2026-09-07 20:11"
    assert render_cell("ORD-1") == "ORD-1"
    assert render_cell(None) == ""
    assert render_cell(10.0) == "10"
    assert render_cell(213.85) == "213.85"


def test_row_and_column_caps_are_reported_not_silent(monkeypatch):
    monkeypatch.setattr("app.generators.tabular.settings.report_max_rows", 2)
    monkeypatch.setattr("app.generators.tabular.settings.report_max_columns", 2)

    table = build_table("orders", _ORDER_ROWS)

    assert len(table.rows) == 2
    assert table.truncated_rows is True
    # `orders` declares a report column order, so the two kept columns are the first two of it
    # that are present -- order_id then amount -- leaving status dropped.
    assert table.dropped_columns == ["status"]
    note = table_note(table)
    assert "2 of 12" in note and "1 further column" in note


def test_geojson_points_render_as_coordinates():
    table = build_table("vendors", [{"location": {"type": "Point", "coordinates": [67.0, 24.8]}}])

    assert table.rows == [["24.8, 67.0"]]


def test_build_tables_skips_empty_domains():
    tables = build_tables({"orders": _ORDER_ROWS, "customers": []})

    assert [t.name for t in tables] == ["orders"]


# --- charts ----------------------------------------------------------------------------------


def test_chart_prefers_a_low_cardinality_dimension_over_an_identifier():
    """Charting against `order_id` gives one bar per order, which communicates nothing."""
    table = build_table("orders", _ORDER_ROWS)
    spec = choose_chart(table)

    assert table.headers[spec.label_column] == "Current Status"
    assert table.headers[spec.value_column] == "Order Payment"


def test_the_question_steers_the_chart_dimension():
    """Someone who asked "by status" wants a chart by status, even when another column has
    fewer distinct values and would win on cardinality alone."""
    rows = [
        {"status": ["pending", "delivered", "cancelled"][i % 3], "city": ["K", "L"][i % 2], "n": i}
        for i in range(1, 13)
    ]
    table = build_table("orders", rows)

    assert table.headers[choose_chart(table, "orders by status").label_column] == "Current Status"
    assert table.headers[choose_chart(table, "orders by city").label_column] == "City"
    # With no question, lowest cardinality decides.
    assert table.headers[choose_chart(table).label_column] == "City"


def test_column_matching_is_by_word_not_substring():
    """ "capacity" contains "cap" but must not select a "City" column."""
    from app.generators.charts import _mentioned_in

    assert _mentioned_in("orders by status", "Status")
    assert not _mentioned_in("show me capacity", "City")


def test_a_filtered_subset_charts_by_the_column_that_defines_it():
    """ "incomplete" names no column, but it *is* a statement about status -- and where those
    orders are stuck is the insight an incomplete-orders report exists to give."""
    from app.generators.charts import _mentioned_in

    assert _mentioned_in("last 10 incomplete order details", "Current Status")
    assert _mentioned_in("pending orders this week", "Current Status")
    # "order" is in both the question and the header, but it's too generic to discriminate --
    # matching on it would mark every column as mentioned.
    assert not _mentioned_in("last 10 incomplete order details", "Order Type")


def test_chart_aggregates_repeated_dimension_values():
    """Raw rows repeat their dimension; without summing, a 3-status pie would get 12 slices."""
    table = build_table("orders", _ORDER_ROWS)
    series = extract_series(table, choose_chart(table))

    assert len(series) == 3
    assert sum(value for _, value in series) == sum(r["amount"] for r in _ORDER_ROWS)


def test_few_positive_categories_become_a_pie():
    table = build_table("orders", [{"status": "a", "n": 3}, {"status": "b", "n": 4}])
    assert choose_chart(table).kind == "pie"


def test_many_categories_become_a_bar():
    table = build_table("orders", [{"city": f"C{i}", "n": i + 1} for i in range(20)])
    assert choose_chart(table).kind == "bar"


def test_negative_values_fall_back_to_a_bar():
    """A pie shows parts of a whole; a negative value has no slice."""
    table = build_table("orders", [{"k": "a", "v": -5}, {"k": "b", "v": 3}])
    assert choose_chart(table).kind == "bar"


def test_temporal_dimension_becomes_a_line():
    rows = [{"day": datetime(2026, 9, d), "amount": d * 10} for d in range(1, 6)]
    assert choose_chart(build_table("orders", rows)).kind == "line"


@pytest.mark.parametrize(
    "rows,reason",
    [
        ([{"status": "ok", "n": 1}], "a single row compares nothing"),
        ([{"a": "x", "b": "y"}, {"a": "p", "b": "q"}], "no numeric measure"),
        ([{"city": "K", "n": 1}, {"city": "K", "n": 2}], "a constant dimension"),
    ],
)
def test_no_chart_when_one_would_say_nothing(rows, reason):
    assert choose_chart(build_table("orders", rows)) is None, reason


def test_chart_renders_png_bytes():
    table = build_table("orders", _ORDER_ROWS)
    png = render_chart_png(table, choose_chart(table))

    assert png.startswith(b"\x89PNG")


# --- CSV -------------------------------------------------------------------------------------


def test_csv_has_a_title_header_row_and_data():
    text = generate_csv(_ROWS_BY_DOMAIN, title="Data Report").decode("utf-8-sig")
    lines = text.splitlines()

    assert lines[0] == "Data Report"
    assert "Orders" in lines
    assert "Order #,Order Payment,Current Status" in lines
    assert "ORD-1,10,delivered" in lines


def test_csv_writes_each_domain_as_its_own_block():
    text = generate_csv(_ROWS_BY_DOMAIN).decode("utf-8-sig")

    assert "Orders" in text and "Vendors" in text
    assert "User ID,Rating" in text


def test_csv_neutralizes_values_excel_would_treat_as_formulas():
    """A stored value beginning with `=` executes on open -- CSV injection."""
    text = generate_csv({"orders": [{"note": "=cmd|'/c calc'!A1"}]}).decode("utf-8-sig")

    assert "'=cmd" in text


def test_csv_handles_no_data():
    assert b"No data" in generate_csv({"orders": []})


# --- XLSX ------------------------------------------------------------------------------------


def _load(rows_by_domain):
    return load_workbook(io.BytesIO(generate_xlsx(rows_by_domain, title="Data Report")))


def test_xlsx_creates_one_titled_sheet_per_domain():
    workbook = _load(_ROWS_BY_DOMAIN)

    assert workbook.sheetnames == ["Orders", "Vendors"]


def test_xlsx_has_a_title_a_header_row_and_native_typed_data():
    sheet = _load(_ROWS_BY_DOMAIN)["Orders"]

    assert sheet["A1"].value == "Orders"
    header_row = next(
        row for row in sheet.iter_rows(values_only=True) if row and row[0] == "Order #"
    )
    assert header_row[:3] == ("Order #", "Order Payment", "Current Status")

    header_index = next(
        i
        for i, row in enumerate(sheet.iter_rows(values_only=True), start=1)
        if row and row[0] == "Order #"
    )
    # Numbers stay numeric so Excel sorts and formats them correctly.
    amounts = [row[1] for row in sheet.iter_rows(min_row=header_index + 1, values_only=True)]
    assert amounts and all(isinstance(a, (int, float)) for a in amounts)


def test_xlsx_freezes_the_header_and_adds_a_filter():
    sheet = _load(_ROWS_BY_DOMAIN)["Orders"]

    assert sheet.freeze_panes is not None
    assert sheet.auto_filter.ref is not None


def test_xlsx_handles_no_data():
    workbook = load_workbook(io.BytesIO(generate_xlsx({"orders": []}, title="Data Report")))

    assert workbook.sheetnames == ["Report"]


# --- PDF -------------------------------------------------------------------------------------


def test_pdf_is_a_valid_document():
    assert (
        generate_pdf(_ROWS_BY_DOMAIN, question="orders by status", answer="Most are pending.")[:5]
        == b"%PDF-"
    )


def test_pdf_reuses_the_supplied_answer_rather_than_asking_the_model_again():
    """The insights block is the answer the pipeline already generated -- a second Gemini call
    would cost quota and risk the PDF disagreeing with the on-screen reply."""
    import app.generators.pdf_generator as module

    rendered = {}
    original = module._TEMPLATE.render
    module._TEMPLATE.render = lambda **kw: rendered.update(kw) or original(**kw)
    try:
        generate_pdf(_ROWS_BY_DOMAIN, question="q", answer="Delivered orders lead.")
    finally:
        module._TEMPLATE.render = original

    assert rendered["insight_paragraphs"] == ["Delivered orders lead."]


def test_pdf_insights_drop_markdown_table_lines():
    """The rows are already in the Data section; repeating them as raw pipes is noise."""
    from app.generators.pdf_generator import _insight_paragraphs

    assert _insight_paragraphs("Summary line.\n\n| a | b |\n|---|---|") == ["Summary line."]


def test_pdf_handles_no_data():
    assert generate_pdf({"orders": []}, question="q", answer="Nothing found.")[:5] == b"%PDF-"
