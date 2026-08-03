"""Safety checks applied to an LLM-generated QuerySpec before it touches MongoDB."""

from datetime import date

from app.rag.query_spec import QuerySpec

BANNED_OPERATORS = {"$where", "$function", "$accumulator", "$merge", "$out"}
ALLOWED_OPERATIONS = {"find", "aggregate", "count"}
MAX_DATE_RANGE_DAYS = 365
NO_DATE_RANGE_LIMIT_CAP = 100
# Clamp for geo_near.max_distance_m, same role as MAX_DATE_RANGE_DAYS -- keeps a "nearby"
# question from silently becoming an unbounded/full-scan geo query.
MAX_GEO_RADIUS_M = 50_000


class QueryValidationError(Exception):
    pass


def _find_violation(value: object, allowed_collections: list[str]) -> str | None:
    if isinstance(value, dict):
        for key, val in value.items():
            if key in BANNED_OPERATORS:
                return f"disallowed operator '{key}'"
            if key == "$lookup" and isinstance(val, dict):
                from_collection = val.get("from")
                if from_collection and from_collection not in allowed_collections:
                    return f"$lookup references disallowed collection '{from_collection}'"
            violation = _find_violation(val, allowed_collections)
            if violation:
                return violation
    elif isinstance(value, list):
        for item in value:
            violation = _find_violation(item, allowed_collections)
            if violation:
                return violation
    return None


def _parse_iso_date(value: str, field_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise QueryValidationError(
            f"invalid {field_name} '{value}': must be an ISO date (YYYY-MM-DD)"
        ) from exc


def _validate_date_range(spec: QuerySpec, max_range_days: int) -> None:
    """Reasons over the *structured* start_date/end_date fields only -- these describe (not
    drive) whatever date filter Gemini embedded in filter/pipeline.

    Both bounds given: span is end - start (and end must not precede start). Exactly one bound
    given: span is measured against today rather than treating the missing bound as literally
    "today" -- an end_date alone is normally a bound in the past (e.g. "orders up to a year ago"),
    so anchoring the missing start to today would make it look like an inverted range instead of
    just a wide one."""
    if spec.start_date is None and spec.end_date is None:
        return

    if spec.start_date and spec.end_date:
        start = _parse_iso_date(spec.start_date, "start_date")
        end = _parse_iso_date(spec.end_date, "end_date")
        if end < start:
            raise QueryValidationError(f"end_date '{end}' is before start_date '{start}'")
        span_days = (end - start).days
    else:
        field_name = "start_date" if spec.start_date else "end_date"
        given = _parse_iso_date(spec.start_date or spec.end_date, field_name)
        span_days = abs((date.today() - given).days)

    if span_days > max_range_days:
        raise QueryValidationError(
            f"date range too long: {span_days} days requested (max {max_range_days})"
        )


def _validate_geo_near(
    spec: QuerySpec, geo_allowed_fields: dict[str, set[str]], max_radius_m: float
) -> None:
    """geo_near is structured data the LLM emits (coordinates + a requested radius), not raw
    Mongo operator syntax -- see app/rag/query_spec.py:GeoNear. Validated the same way dates
    are: reject what's out of scope, clamp what's just too broad."""
    if spec.geo_near is None:
        return

    allowed_fields = geo_allowed_fields.get(spec.collection, set())
    if not allowed_fields:
        raise QueryValidationError(f"collection '{spec.collection}' does not support geo queries")
    if spec.geo_near.field not in allowed_fields:
        raise QueryValidationError(
            f"geo field '{spec.geo_near.field}' is not recognized on '{spec.collection}'"
        )
    if spec.geo_near.max_distance_m <= 0:
        raise QueryValidationError("geo_near.max_distance_m must be positive")
    spec.geo_near.max_distance_m = min(spec.geo_near.max_distance_m, max_radius_m)


def validate_query_spec(
    spec: QuerySpec,
    allowed_collections: list[str],
    max_limit: int = 200,
    max_date_range_days: int = MAX_DATE_RANGE_DAYS,
    no_date_range_limit_cap: int = NO_DATE_RANGE_LIMIT_CAP,
    geo_allowed_fields: dict[str, set[str]] | None = None,
    max_geo_radius_m: float = MAX_GEO_RADIUS_M,
) -> QuerySpec:
    """Raise QueryValidationError on anything unsafe or out of scope; clamp the limit otherwise."""
    if spec.collection not in allowed_collections:
        raise QueryValidationError(f"collection '{spec.collection}' is not allowed")

    if spec.operation not in ALLOWED_OPERATIONS:
        raise QueryValidationError(f"operation '{spec.operation}' is not allowed")

    violation = (
        _find_violation(spec.filter, allowed_collections)
        or _find_violation(spec.pipeline, allowed_collections)
        or _find_violation(spec.projection or {}, allowed_collections)
    )
    if violation:
        raise QueryValidationError(violation)

    _validate_date_range(spec, max_date_range_days)
    _validate_geo_near(spec, geo_allowed_fields or {}, max_geo_radius_m)

    no_dates = spec.start_date is None and spec.end_date is None
    effective_cap = min(max_limit, no_date_range_limit_cap) if no_dates else max_limit
    spec.limit = min(max(spec.limit, 1), effective_cap)
    return spec
