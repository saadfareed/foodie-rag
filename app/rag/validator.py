"""Safety checks applied to an LLM-generated QuerySpec before it touches MongoDB."""

from app.rag.query_spec import QuerySpec

BANNED_OPERATORS = {"$where", "$function", "$accumulator", "$merge", "$out"}
ALLOWED_OPERATIONS = {"find", "aggregate", "count"}


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


def validate_query_spec(
    spec: QuerySpec, allowed_collections: list[str], max_limit: int = 200
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

    spec.limit = min(max(spec.limit, 1), max_limit)
    return spec
