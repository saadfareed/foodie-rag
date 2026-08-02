from app.rag.calculation import average, count, maximum, minimum, total

ROWS = [
    {"amount": 10, "note": "a"},
    {"amount": 20, "note": "b"},
    {"amount": 30, "note": None},
    {"note": "missing amount"},
]


def test_total_sums_numeric_fields_only():
    assert total(ROWS, "amount") == 60


def test_average_ignores_missing_and_non_numeric():
    assert average(ROWS, "amount") == 20


def test_minimum_and_maximum():
    assert minimum(ROWS, "amount") == 10
    assert maximum(ROWS, "amount") == 30


def test_count_counts_rows_not_field_presence():
    assert count(ROWS) == 4


def test_empty_rows_return_none_or_zero():
    assert total([], "amount") == 0
    assert average([], "amount") is None
    assert minimum([], "amount") is None
    assert maximum([], "amount") is None
    assert count([]) == 0


def test_non_numeric_values_are_ignored():
    rows = [{"amount": "not a number"}, {"amount": 5}]
    assert total(rows, "amount") == 5
    assert average(rows, "amount") == 5
