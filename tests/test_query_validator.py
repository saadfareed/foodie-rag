import pytest

from app.rag.query_spec import QuerySpec
from app.rag.validator import QueryValidationError, validate_query_spec

ALLOWED = ["orders"]


def test_valid_find_spec_passes():
    spec = QuerySpec(collection="orders", operation="find", filter={"status": "shipped"})
    result = validate_query_spec(spec, ALLOWED)
    assert result.collection == "orders"


def test_disallowed_collection_rejected():
    spec = QuerySpec(collection="users", operation="find")
    with pytest.raises(QueryValidationError, match="not allowed"):
        validate_query_spec(spec, ALLOWED)


def test_banned_operator_in_filter_rejected():
    spec = QuerySpec(collection="orders", operation="find", filter={"$where": "this.a == this.b"})
    with pytest.raises(QueryValidationError, match="disallowed operator"):
        validate_query_spec(spec, ALLOWED)


def test_banned_stage_in_pipeline_rejected():
    spec = QuerySpec(
        collection="orders",
        operation="aggregate",
        pipeline=[{"$match": {}}, {"$merge": {"into": "orders"}}],
    )
    with pytest.raises(QueryValidationError, match="disallowed operator"):
        validate_query_spec(spec, ALLOWED)


def test_lookup_to_disallowed_collection_rejected():
    lookup_stage = {
        "$lookup": {"from": "secrets", "localField": "a", "foreignField": "b", "as": "x"}
    }
    spec = QuerySpec(collection="orders", operation="aggregate", pipeline=[lookup_stage])
    with pytest.raises(QueryValidationError, match="disallowed collection"):
        validate_query_spec(spec, ALLOWED)


def test_lookup_to_allowed_collection_passes():
    lookup_stage = {
        "$lookup": {"from": "orders", "localField": "a", "foreignField": "b", "as": "x"}
    }
    spec = QuerySpec(collection="orders", operation="aggregate", pipeline=[lookup_stage])
    validate_query_spec(spec, ALLOWED)


def test_limit_clamped_to_max():
    spec = QuerySpec(collection="orders", operation="find", limit=100_000)
    result = validate_query_spec(spec, ALLOWED, max_limit=200)
    assert result.limit == 200


def test_limit_clamped_to_min():
    spec = QuerySpec(collection="orders", operation="find", limit=0)
    result = validate_query_spec(spec, ALLOWED, max_limit=200)
    assert result.limit == 1


def test_invalid_operation_rejected():
    with pytest.raises(ValueError):
        QuerySpec(collection="orders", operation="delete")
