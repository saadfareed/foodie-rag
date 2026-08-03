import pytest

from app.agents.domains import (
    DOMAINS,
    allowed_collections,
    geo_allowed_fields,
    merge_forced_filter,
    scope_spec_to_domain,
)
from app.rag.query_spec import QuerySpec


def test_allowed_collections_is_the_deduplicated_set_of_domain_collections():
    assert allowed_collections() == ["orders", "users"]


def test_geo_allowed_fields_maps_users_to_location_only():
    assert geo_allowed_fields() == {"users": {"location"}}


def test_scope_spec_forces_collection_regardless_of_llm_output():
    spec = QuerySpec(collection="anything-the-model-made-up", operation="find")
    scoped = scope_spec_to_domain(spec, DOMAINS["vendors"])
    assert scoped.collection == "users"


def test_scope_spec_injects_usertype_into_empty_filter():
    spec = QuerySpec(collection="users", operation="find", filter={})
    scoped = scope_spec_to_domain(spec, DOMAINS["vendors"])
    assert scoped.filter == {"usertype": 2}


def test_scope_spec_wraps_existing_filter_with_forced_usertype():
    spec = QuerySpec(collection="users", operation="find", filter={"status": "active"})
    scoped = scope_spec_to_domain(spec, DOMAINS["customers"])
    assert scoped.filter == {"$and": [{"usertype": 1}, {"status": "active"}]}


def test_scope_spec_ignores_a_usertype_the_model_tried_to_set_itself():
    """The model is told never to set usertype itself (see app/agents/query_agents.py), but even
    if it does, the forced value must win -- this is the guardrail, not a suggestion."""
    spec = QuerySpec(collection="users", operation="find", filter={"usertype": 1})
    scoped = scope_spec_to_domain(spec, DOMAINS["vendors"])
    assert scoped.filter == {"$and": [{"usertype": 2}, {"usertype": 1}]}


def test_scope_spec_prepends_match_stage_for_aggregate():
    spec = QuerySpec(collection="users", operation="aggregate", pipeline=[{"$sort": {"name": 1}}])
    scoped = scope_spec_to_domain(spec, DOMAINS["vendors"])
    assert scoped.pipeline == [{"$match": {"usertype": 2}}, {"$sort": {"name": 1}}]


def test_scope_spec_is_a_noop_for_domains_without_a_usertype():
    spec = QuerySpec(collection="orders", operation="find", filter={"status": "pending"})
    scoped = scope_spec_to_domain(spec, DOMAINS["orders"])
    assert scoped.filter == {"status": "pending"}


@pytest.mark.parametrize("domain_name", ["orders", "customers", "vendors"])
def test_scope_spec_always_sets_the_domains_own_collection(domain_name):
    domain = DOMAINS[domain_name]
    spec = QuerySpec(collection="whatever", operation="find")
    assert scope_spec_to_domain(spec, domain).collection == domain.collection


def test_merge_forced_filter_returns_spec_unchanged_when_forced_is_empty():
    spec = QuerySpec(collection="orders", operation="find", filter={"status": "pending"})
    merge_forced_filter(spec, {})
    assert spec.filter == {"status": "pending"}


def test_merge_forced_filter_on_aggregate_prepends_match():
    spec = QuerySpec(collection="orders", operation="aggregate", pipeline=[{"$limit": 5}])
    merge_forced_filter(spec, {"vendor_id": {"$in": ["V1"]}})
    assert spec.pipeline == [{"$match": {"vendor_id": {"$in": ["V1"]}}}, {"$limit": 5}]
