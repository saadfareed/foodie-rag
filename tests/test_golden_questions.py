"""Table-driven regression tests for the *deterministic* half of the agent pipeline: given a raw
(unscoped, unvalidated) QuerySpec shape a model might plausibly produce for a domain, does
scope_spec_to_domain + validate_query_spec always land on the expected, safe final spec?

A live LLM call isn't deterministic enough to golden-test cheaply/reliably in normal CI -- actual
prompt-drift detection (does the live model still classify "active vendors nearby" as
`vendors`+geo) is tests/test_live_classifier.py instead, opt-in and skipped by default. What's
tested here is the part that must never depend on model behavior at all: no matter what a model
writes, a "vendors" question can only ever read usertype=2 rows from `users`, geo queries get
clamped to a sane radius, and a domain without a geo field can't be given one.
"""

import pytest

from app.agents.domains import (
    DOMAINS,
    allowed_collections,
    geo_allowed_fields,
    scope_spec_to_domain,
)
from app.rag.query_spec import GeoNear, QuerySpec
from app.rag.validator import QueryValidationError, validate_query_spec

CASES = [
    # (domain, raw spec kwargs a hypothetical model produced, expected final collection+filter)
    (
        "orders",
        {"collection": "orders", "operation": "find", "filter": {"status": "pending"}},
        "orders",
        {"status": "pending"},
    ),
    (
        "customers",
        {"collection": "made-up-collection", "operation": "find", "filter": {}},
        "users",
        {"usertype": 1},
    ),
    (
        "vendors",
        {"collection": "users", "operation": "find", "filter": {"city": "Karachi"}},
        "users",
        {"$and": [{"usertype": 2}, {"city": "Karachi"}]},
    ),
    # A model that tries to set usertype itself must still be overridden by the forced value.
    (
        "vendors",
        {"collection": "users", "operation": "find", "filter": {"usertype": 1}},
        "users",
        {"$and": [{"usertype": 2}, {"usertype": 1}]},
    ),
]


@pytest.mark.parametrize("domain_name,raw,expected_collection,expected_filter", CASES)
def test_scope_and_validate_produces_the_expected_final_spec(
    domain_name, raw, expected_collection, expected_filter
):
    spec = scope_spec_to_domain(QuerySpec(**raw), DOMAINS[domain_name])
    validated = validate_query_spec(
        spec, allowed_collections(), geo_allowed_fields=geo_allowed_fields()
    )
    assert validated.collection == expected_collection
    assert validated.filter == expected_filter


def test_vendor_geo_query_is_clamped_to_the_max_radius():
    spec = scope_spec_to_domain(
        QuerySpec(
            collection="users",
            operation="find",
            geo_near=GeoNear(field="location", longitude=1, latitude=2, max_distance_m=10_000_000),
        ),
        DOMAINS["vendors"],
    )
    validated = validate_query_spec(
        spec,
        allowed_collections(),
        geo_allowed_fields=geo_allowed_fields(),
        max_geo_radius_m=50_000,
    )
    assert validated.geo_near.max_distance_m == 50_000


def test_orders_domain_cannot_be_given_a_geo_query():
    spec = scope_spec_to_domain(
        QuerySpec(
            collection="orders",
            operation="find",
            geo_near=GeoNear(field="location", longitude=1, latitude=2, max_distance_m=1000),
        ),
        DOMAINS["orders"],
    )
    with pytest.raises(QueryValidationError, match="does not support geo queries"):
        validate_query_spec(spec, allowed_collections(), geo_allowed_fields=geo_allowed_fields())


def test_a_hallucinated_domain_name_cannot_be_scoped_at_all():
    """DOMAINS is a plain dict keyed by DOMAIN_NAMES -- a domain name outside that set raises a
    KeyError immediately rather than silently falling through to some default collection."""
    with pytest.raises(KeyError):
        DOMAINS["secrets"]
