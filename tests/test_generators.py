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


# --- what the chart measures ---------------------------------------------------------------------


def test_a_how_many_question_counts_rows_rather_than_summing_money():
    """The production bug this exists for. Three orders, one in each status, charted from an
    orders table whose first numeric column is `amount` -- so "how many orders are incomplete"
    drew 47.2% / 42.5% / 10.3%, which is how the *money* split. Every correct answer was 33.3%.
    A chart answering a different question than the one asked is worse than no chart, because
    nothing about it looks wrong.
    """
    rows = [
        {"order_id": "ORD-1", "amount": 373.77, "status": "preparing"},
        {"order_id": "ORD-2", "amount": 336.60, "status": "pending"},
        {"order_id": "ORD-3", "amount": 81.60, "status": "refunded"},
    ]
    table = build_table("orders", rows)

    spec = choose_chart(table, "how many orders are incomplete and what are their current status")

    assert spec.value_column is None, "a count question measures rows, not a column"
    series = extract_series(table, spec)
    assert sorted(value for _, value in series) == [1, 1, 1]
    assert {label for label, _ in series} == {"preparing", "pending", "refunded"}


def test_a_counted_chart_is_named_for_the_rows_not_a_column():
    """It deliberately isn't reading `amount`, so calling itself "Amount by Status" would be a
    label for a chart it didn't draw."""
    table = build_table(
        "orders", [{"amount": 10.0, "status": "a"}, {"amount": 90.0, "status": "b"}]
    )

    # "Orders" is the table, "Current Status" is what `status` is called in a report
    # (app/generators/tabular.py::_HEADER_OVERRIDES).
    assert choose_chart(table, "how many orders by status").title == "Orders by Current Status"


@pytest.mark.parametrize(
    "question",
    ["total amount by status", "how much did each status account for", "orders by status in pdf"],
)
def test_a_question_that_is_not_about_counting_still_sums(question):
    """ "How much" is a sum question, and so is a plain request for a breakdown. Counting those
    would be the same bug pointed the other way."""
    rows = [
        {"amount": 373.77, "status": "preparing"},
        {"amount": 336.60, "status": "pending"},
    ]
    table = build_table("orders", rows)

    spec = choose_chart(table, question)

    assert spec.value_column is not None
    assert table.headers[spec.value_column] == "Order Payment", "`amount`, as a report names it"


def test_a_count_chart_works_when_there_is_no_numeric_column_at_all():
    """Previously this drew nothing: no numeric column meant no measure. Counting rows is a
    measure, and "how many customers per city" is a perfectly good chart."""
    table = build_table("customers", [{"city": "Karachi"}, {"city": "Lahore"}, {"city": "Karachi"}])

    spec = choose_chart(table, "how many customers in each city")

    assert spec is not None
    assert sorted(value for _, value in extract_series(table, spec)) == [1, 2]


def test_counting_still_refuses_to_chart_against_an_identifier():
    """A count by `order_id` is one bar per order saying 1 -- the same nothing the cardinality
    rule exists to prevent, and counting must not become a way around it."""
    rows = [{"order_id": f"ORD-{i}", "status": ["a", "b"][i % 2]} for i in range(6)]
    table = build_table("orders", rows)

    spec = choose_chart(table, "how many orders")

    assert table.headers[spec.label_column] == "Current Status"


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


# --- chart rendering branches --------------------------------------------------------------


def test_bar_chart_renders():
    table = build_table("orders", [{"city": f"C{i}", "n": i + 1} for i in range(20)])
    spec = choose_chart(table)

    assert spec.kind == "bar"
    assert render_chart_png(table, spec).startswith(b"\x89PNG")


def test_line_chart_renders():
    rows = [{"day": datetime(2026, 9, d), "amount": d * 10} for d in range(1, 6)]
    table = build_table("orders", rows)
    spec = choose_chart(table)

    assert spec.kind == "line"
    assert render_chart_png(table, spec).startswith(b"\x89PNG")


def test_render_chart_base64_returns_none_when_no_chart_suits_the_table():
    table = build_table("orders", [{"a": "x", "b": "y"}, {"a": "p", "b": "q"}])

    from app.generators.charts import render_chart_base64

    assert render_chart_base64(table) is None


def test_a_plotting_failure_costs_the_chart_not_the_document(monkeypatch):
    """The chart is a nice-to-have on a report whose table is the actual answer."""
    from app.generators import charts

    monkeypatch.setattr(
        charts, "render_chart_png", lambda *a, **kw: (_ for _ in ()).throw(ValueError("bad"))
    )
    table = build_table("orders", _ORDER_ROWS)

    assert charts.render_chart_base64(table) is None


def test_extract_series_skips_rows_with_a_non_numeric_measure():
    table = build_table("orders", [{"k": "a", "v": 1}, {"k": "b", "v": None}, {"k": "c", "v": 3}])
    spec = choose_chart(table)

    assert dict(extract_series(table, spec)) == {"a": 1.0, "c": 3.0}


# --- value flattening ----------------------------------------------------------------------


def test_flatten_value_renders_a_nested_document_compactly():
    from app.generators.tabular import flatten_value

    assert flatten_value({"wallet": 40, "card": 60}) == "wallet=40, card=60"


def test_flatten_value_joins_a_list():
    from app.generators.tabular import flatten_value

    assert flatten_value(["a", "b"]) == "a, b"


def test_flatten_value_keeps_numbers_native_for_the_spreadsheet():
    """XLSX needs real numbers to sort and format them; the chart layer needs them to do
    arithmetic."""
    from app.generators.tabular import flatten_value

    assert flatten_value(12.5) == 12.5
    assert flatten_value(True) is True
    assert flatten_value(None) is None


def test_columns_fall_back_to_first_seen_order_without_a_declared_preference():
    """`build_table` is also used for ad-hoc names that aren't registered domains."""
    table = build_table("unknown_domain", [{"z": 1, "a": 2}])

    assert table.headers == ["Z", "A"]


# --- truncation is visible in every format ---------------------------------------------------
#
# A reader who isn't told will treat a capped table as the complete result set. Each writer
# emits the note separately, so each needs its own check.


def test_csv_states_when_rows_were_capped(monkeypatch):
    monkeypatch.setattr("app.generators.tabular.settings.report_max_rows", 3)

    text = generate_csv({"orders": _ORDER_ROWS}).decode("utf-8-sig")

    assert "Showing first 3 of 12 rows" in text


def test_xlsx_states_when_rows_were_capped(monkeypatch):
    monkeypatch.setattr("app.generators.tabular.settings.report_max_rows", 3)

    sheet = load_workbook(io.BytesIO(generate_xlsx({"orders": _ORDER_ROWS})))["Orders"]
    values = [row[0] for row in sheet.iter_rows(values_only=True)]

    assert any(v and "Showing first 3 of 12 rows" in str(v) for v in values)


def test_pdf_states_when_rows_were_capped(monkeypatch):
    monkeypatch.setattr("app.generators.tabular.settings.report_max_rows", 3)
    captured = {}
    import app.generators.pdf_generator as pdf_module

    original = pdf_module._TEMPLATE.render
    monkeypatch.setattr(
        pdf_module._TEMPLATE, "render", lambda **kw: captured.update(kw) or original(**kw)
    )

    generate_pdf({"orders": _ORDER_ROWS}, question="q", answer="a")

    assert "Showing first 3 of 12 rows" in captured["tables"][0]["note"]


def test_xlsx_disambiguates_colliding_sheet_names():
    """Excel rejects duplicate sheet names outright, so a collision must be renamed, not raised."""
    long_name = "a" * 40
    workbook = load_workbook(
        io.BytesIO(generate_xlsx({long_name: [{"a": 1}], long_name + "b": [{"a": 2}]}))
    )

    assert len(workbook.sheetnames) == 2
    assert len(set(workbook.sheetnames)) == 2
    assert all(len(name) <= 31 for name in workbook.sheetnames)


# --- defensive branches ----------------------------------------------------------------------


def test_build_table_returns_none_for_rows_with_no_columns():
    assert build_table("orders", [{}, {}]) is None


def test_render_cell_handles_native_date_and_datetime_objects():
    from datetime import date, timezone

    assert render_cell(datetime(2026, 9, 7, 20, 11, tzinfo=timezone.utc)) == "2026-09-07 20:11"
    assert render_cell(date(2026, 9, 7)) == "2026-09-07"


def test_render_cell_leaves_an_unparseable_datetime_like_string_alone():
    """Shape-matches the ISO pattern but isn't a real date -- better shown as-is than crashing
    the whole report."""
    assert render_cell("2026-13-45 99:99:99") == "2026-13-45 99:99:99"


def test_flatten_value_stringifies_an_unknown_type():
    from decimal import Decimal

    from app.generators.tabular import flatten_value

    assert flatten_value(Decimal("1.5")) == "1.5"


def test_humanize_header_preserves_an_acronym():
    """Title-casing an already-uppercase word would turn VAT into "Vat"."""
    assert humanize_header("VAT amount") == "VAT Amount"
    assert humanize_header("gps") == "Gps"


def test_chart_ignores_a_column_that_is_entirely_null():
    """An all-null column has no values to rank, so it can't be the dimension."""
    table = build_table("orders", [{"k": None, "s": "a", "n": 1}, {"k": None, "s": "b", "n": 2}])
    spec = choose_chart(table)

    assert table.headers[spec.label_column] == "S"


def test_render_chart_png_rejects_a_table_with_nothing_plottable():
    from app.generators.charts import ChartSpec

    table = build_table("orders", [{"a": "x", "b": "y"}, {"a": "p", "b": "q"}])
    spec = ChartSpec(kind="bar", label_column=0, value_column=1, title="t")

    with pytest.raises(ValueError, match="no plottable pairs"):
        render_chart_png(table, spec)


def test_pdf_insights_are_empty_when_there_is_no_answer():
    from app.generators.pdf_generator import _insight_paragraphs

    assert _insight_paragraphs("") == []
    assert _insight_paragraphs(None) == []


def test_no_chart_when_aggregation_collapses_to_one_category():
    """Two rows sharing a dimension value sum into a single bar, which compares nothing."""
    table = build_table("orders", [{"s": "pending", "n": 1}, {"s": "pending", "n": 2}])

    assert choose_chart(table) is None


def test_no_chart_when_only_one_row_has_a_usable_measure():
    """Two distinct categories, but only one carries a number -- after aggregation there is a
    single bar, which compares nothing."""
    table = build_table("orders", [{"s": "a", "n": 1}, {"s": "b", "n": None}])

    assert choose_chart(table) is None


def test_extract_series_skips_a_short_row():
    """Rows are built column-by-column, but a defensive guard keeps a ragged row from raising
    IndexError mid-render."""
    from app.generators.charts import ChartSpec, extract_series
    from app.generators.tabular import ReportTable

    table = ReportTable(
        name="orders", headers=["S", "N"], rows=[["a", 1], ["b"]], total_row_count=2
    )
    spec = ChartSpec(kind="bar", label_column=0, value_column=1, title="t")

    assert extract_series(table, spec) == [("a", 1.0)]
