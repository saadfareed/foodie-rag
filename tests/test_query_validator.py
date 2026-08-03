from datetime import date, timedelta

import pytest

from app.rag.query_spec import GeoNear, QuerySpec
from app.rag.validator import (
    MAX_DATE_RANGE_DAYS,
    MAX_GEO_RADIUS_M,
    NO_DATE_RANGE_LIMIT_CAP,
    QueryValidationError,
    validate_query_spec,
)

ALLOWED = ["orders"]
ALLOWED_WITH_USERS = ["orders", "users"]
GEO_FIELDS = {"users": {"location"}}


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
    # Dates provided so the new no-date-range cap doesn't shadow this assertion -- the no-date
    # case is covered separately by test_no_dates_caps_limit_to_100.
    spec = QuerySpec(
        collection="orders",
        operation="find",
        limit=100_000,
        start_date=(date.today() - timedelta(days=10)).isoformat(),
        end_date=date.today().isoformat(),
    )
    result = validate_query_spec(spec, ALLOWED, max_limit=200)
    assert result.limit == 200


def test_limit_clamped_to_min():
    spec = QuerySpec(collection="orders", operation="find", limit=0)
    result = validate_query_spec(spec, ALLOWED, max_limit=200)
    assert result.limit == 1


def test_invalid_operation_rejected():
    with pytest.raises(ValueError):
        QuerySpec(collection="orders", operation="delete")


def test_no_dates_caps_limit_to_100():
    spec = QuerySpec(collection="orders", operation="find", limit=150)
    result = validate_query_spec(spec, ALLOWED)
    assert result.limit == NO_DATE_RANGE_LIMIT_CAP


def test_no_dates_does_not_raise_an_already_smaller_limit():
    spec = QuerySpec(collection="orders", operation="find", limit=10)
    result = validate_query_spec(spec, ALLOWED)
    assert result.limit == 10


def test_dates_given_allows_limit_above_100_up_to_max_limit():
    spec = QuerySpec(
        collection="orders",
        operation="find",
        limit=150,
        start_date=(date.today() - timedelta(days=10)).isoformat(),
        end_date=date.today().isoformat(),
    )
    result = validate_query_spec(spec, ALLOWED, max_limit=200)
    assert result.limit == 150


def test_only_start_date_within_365_days_of_today_passes():
    spec = QuerySpec(
        collection="orders",
        operation="find",
        start_date=(date.today() - timedelta(days=30)).isoformat(),
    )
    validate_query_spec(spec, ALLOWED)


def test_only_start_date_more_than_365_days_ago_rejected():
    spec = QuerySpec(
        collection="orders",
        operation="find",
        start_date=(date.today() - timedelta(days=400)).isoformat(),
    )
    with pytest.raises(QueryValidationError, match="date range too long"):
        validate_query_spec(spec, ALLOWED)


def test_only_end_date_more_than_365_days_ago_rejected():
    spec = QuerySpec(
        collection="orders",
        operation="find",
        end_date=(date.today() - timedelta(days=400)).isoformat(),
    )
    with pytest.raises(QueryValidationError, match="date range too long"):
        validate_query_spec(spec, ALLOWED)


def test_both_dates_within_365_days_passes():
    spec = QuerySpec(
        collection="orders",
        operation="find",
        start_date=(date.today() - timedelta(days=300)).isoformat(),
        end_date=date.today().isoformat(),
    )
    validate_query_spec(spec, ALLOWED)


def test_both_dates_span_exceeding_365_days_rejected():
    spec = QuerySpec(
        collection="orders",
        operation="find",
        start_date=(date.today() - timedelta(days=400)).isoformat(),
        end_date=date.today().isoformat(),
    )
    with pytest.raises(QueryValidationError, match="date range too long"):
        validate_query_spec(spec, ALLOWED)


def test_end_date_before_start_date_rejected():
    spec = QuerySpec(
        collection="orders",
        operation="find",
        start_date=date.today().isoformat(),
        end_date=(date.today() - timedelta(days=10)).isoformat(),
    )
    with pytest.raises(QueryValidationError, match="before start_date"):
        validate_query_spec(spec, ALLOWED)


def test_invalid_start_date_format_rejected():
    spec = QuerySpec(collection="orders", operation="find", start_date="not-a-date")
    with pytest.raises(QueryValidationError, match="invalid start_date"):
        validate_query_spec(spec, ALLOWED)


def test_invalid_end_date_format_rejected():
    spec = QuerySpec(collection="orders", operation="find", end_date="not-a-date")
    with pytest.raises(QueryValidationError, match="invalid end_date"):
        validate_query_spec(spec, ALLOWED)


def test_custom_max_date_range_days_parameter_respected():
    spec = QuerySpec(
        collection="orders",
        operation="find",
        start_date=(date.today() - timedelta(days=60)).isoformat(),
        end_date=date.today().isoformat(),
    )
    with pytest.raises(QueryValidationError, match="date range too long"):
        validate_query_spec(spec, ALLOWED, max_date_range_days=30)


def test_default_max_date_range_days_constant_is_365():
    assert MAX_DATE_RANGE_DAYS == 365


def _geo_spec(max_distance_m=1000.0, field="location", collection="users"):
    return QuerySpec(
        collection=collection,
        operation="find",
        geo_near=GeoNear(field=field, longitude=67.0, latitude=24.8, max_distance_m=max_distance_m),
    )


def test_geo_near_on_a_geo_capable_collection_passes():
    result = validate_query_spec(_geo_spec(), ALLOWED_WITH_USERS, geo_allowed_fields=GEO_FIELDS)
    assert result.geo_near.max_distance_m == 1000.0


def test_geo_near_is_clamped_to_max_radius():
    result = validate_query_spec(
        _geo_spec(max_distance_m=999_999_999),
        ALLOWED_WITH_USERS,
        geo_allowed_fields=GEO_FIELDS,
        max_geo_radius_m=50_000,
    )
    assert result.geo_near.max_distance_m == 50_000


def test_geo_near_on_a_non_geo_capable_collection_rejected():
    spec = _geo_spec(collection="orders")
    with pytest.raises(QueryValidationError, match="does not support geo queries"):
        validate_query_spec(spec, ALLOWED, geo_allowed_fields={})


def test_geo_near_with_unrecognized_field_rejected():
    spec = _geo_spec(field="not_a_real_field")
    with pytest.raises(QueryValidationError, match="not recognized"):
        validate_query_spec(spec, ALLOWED_WITH_USERS, geo_allowed_fields=GEO_FIELDS)


def test_geo_near_with_non_positive_distance_rejected():
    spec = _geo_spec(max_distance_m=0)
    with pytest.raises(QueryValidationError, match="must be positive"):
        validate_query_spec(spec, ALLOWED_WITH_USERS, geo_allowed_fields=GEO_FIELDS)


def test_spec_without_geo_near_is_unaffected_by_geo_validation():
    spec = QuerySpec(collection="orders", operation="find")
    result = validate_query_spec(spec, ALLOWED)
    assert result.geo_near is None


def test_default_max_geo_radius_m_constant_is_50000():
    assert MAX_GEO_RADIUS_M == 50_000
