"""Pure helper functions for computing over query results in Python.

Prefer pushing aggregation into MongoDB ($group/$sum/etc.) where possible; these
exist for cases where post-processing a small result set is simpler.
"""

from numbers import Number


def _numeric_values(rows: list[dict], field: str) -> list[float]:
    return [row[field] for row in rows if isinstance(row.get(field), Number)]


def total(rows: list[dict], field: str) -> float:
    return sum(_numeric_values(rows, field))


def average(rows: list[dict], field: str) -> float | None:
    values = _numeric_values(rows, field)
    return sum(values) / len(values) if values else None


def minimum(rows: list[dict], field: str) -> float | None:
    values = _numeric_values(rows, field)
    return min(values) if values else None


def maximum(rows: list[dict], field: str) -> float | None:
    values = _numeric_values(rows, field)
    return max(values) if values else None


def count(rows: list[dict]) -> int:
    return len(rows)
