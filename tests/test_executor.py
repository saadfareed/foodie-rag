from datetime import datetime

import pytest
from bson import ObjectId

from app.db.executor import execute_query_spec
from app.rag.query_spec import GeoNear, QuerySpec


class _FakeCursor(list):
    def limit(self, n):
        return self

    def max_time_ms(self, n):
        return self


class _FakeCollection:
    def __init__(self, rows=None):
        self._rows = rows or []
        self.find_calls = []
        self.aggregate_calls = []
        self.count_calls = []
        self.estimated_calls = []

    def find(self, filter_, projection=None, sort=None):
        self.find_calls.append(filter_)
        return _FakeCursor(self._rows)

    def aggregate(self, pipeline, maxTimeMS=None):
        self.aggregate_calls.append(pipeline)
        return _FakeCursor(self._rows)

    def count_documents(self, filter_, maxTimeMS=None):
        self.count_calls.append(filter_)
        return len(self._rows)

    def estimated_document_count(self, maxTimeMS=None):
        self.estimated_calls.append(maxTimeMS)
        return 9999


class _FakeDb(dict):
    def __getitem__(self, name):
        return dict.__getitem__(self, name)


def test_to_jsonable_converts_datetime_and_drops_internal_id():
    collection = _FakeCollection([{"_id": ObjectId(), "created_at": datetime(2024, 1, 1)}])
    db = _FakeDb(orders=collection)
    spec = QuerySpec(collection="orders", operation="find")

    rows = execute_query_spec(db, spec)

    assert isinstance(rows[0]["created_at"], str)
    # _id is storage plumbing, dropped by the field policy on the way out of the executor -- see
    # app/security/field_policy.py. It never reaches the model, a file, or the user.
    assert "_id" not in rows[0]


def test_secret_valued_fields_are_redacted_not_returned():
    collection = _FakeCollection([{"user_id": "USR-1", "card_number": "4111111111111111"}])
    db = _FakeDb(users=collection)
    spec = QuerySpec(collection="users", operation="find")

    rows = execute_query_spec(db, spec)

    assert rows[0]["user_id"] == "USR-1"
    assert "4111" not in str(rows[0]["card_number"])


def test_find_without_geo_near_uses_filter_unchanged():
    collection = _FakeCollection([{"amount": 10}])
    db = _FakeDb(orders=collection)
    spec = QuerySpec(collection="orders", operation="find", filter={"status": "pending"})

    execute_query_spec(db, spec)

    assert collection.find_calls == [{"status": "pending"}]


def test_find_with_geo_near_merges_near_clause_into_filter():
    collection = _FakeCollection([{"user_id": "USR-1"}])
    db = _FakeDb(users=collection)
    spec = QuerySpec(
        collection="users",
        operation="find",
        filter={"usertype": 2},
        geo_near=GeoNear(field="location", longitude=67.0, latitude=24.8, max_distance_m=5000),
    )

    execute_query_spec(db, spec)

    filter_ = collection.find_calls[0]
    assert filter_["usertype"] == 2
    assert filter_["location"]["$near"]["$geometry"] == {
        "type": "Point",
        "coordinates": [67.0, 24.8],
    }
    assert filter_["location"]["$near"]["$maxDistance"] == 5000


def test_count_with_geo_near_merges_near_clause_into_filter():
    collection = _FakeCollection([{}, {}])
    db = _FakeDb(users=collection)
    spec = QuerySpec(
        collection="users",
        operation="count",
        geo_near=GeoNear(field="location", longitude=1.0, latitude=2.0, max_distance_m=100),
    )

    result = execute_query_spec(db, spec)

    assert result == [{"count": 2}]
    assert "location" in collection.count_calls[0]


def test_aggregate_without_geo_near_appends_limit_stage_only():
    collection = _FakeCollection([])
    db = _FakeDb(orders=collection)
    spec = QuerySpec(
        collection="orders", operation="aggregate", pipeline=[{"$match": {}}], limit=25
    )

    execute_query_spec(db, spec)

    assert collection.aggregate_calls[0] == [{"$match": {}}, {"$limit": 25}]


def test_aggregate_with_geo_near_prepends_geo_near_stage_first():
    """$geoNear must be the very first pipeline stage -- even ahead of a usertype $match
    prepended by scope_spec_to_domain (app/agents/domains.py)."""
    collection = _FakeCollection([])
    db = _FakeDb(users=collection)
    spec = QuerySpec(
        collection="users",
        operation="aggregate",
        pipeline=[{"$match": {"usertype": 2}}],
        limit=10,
        geo_near=GeoNear(field="location", longitude=3.0, latitude=4.0, max_distance_m=2000),
    )

    execute_query_spec(db, spec)

    pipeline = collection.aggregate_calls[0]
    assert list(pipeline[0].keys()) == ["$geoNear"]
    assert pipeline[0]["$geoNear"]["near"] == {"type": "Point", "coordinates": [3.0, 4.0]}
    assert pipeline[0]["$geoNear"]["maxDistance"] == 2000
    assert pipeline[1] == {"$match": {"usertype": 2}}
    assert pipeline[-1] == {"$limit": 10}


# --- $limit pushdown ---------------------------------------------------------------------
#
# One test per branch of _build_pipeline. The trailing limit bounds what crosses the wire; the
# pushed-down one bounds what the server actually reads. Getting the placement wrong is silent
# in both directions -- too late and it does nothing, too early and it returns the wrong rows.


def _pipeline_for(stages, limit=50):
    collection = _FakeCollection([{"a": 1}])
    db = _FakeDb(orders=collection)
    spec = QuerySpec(collection="orders", operation="aggregate", pipeline=stages, limit=limit)

    execute_query_spec(db, spec)

    return collection.aggregate_calls[0]


def test_limit_is_pushed_down_past_a_leading_match():
    """The scan stops at n documents instead of reading everything and truncating at the end."""
    pipeline = _pipeline_for([{"$match": {"usertype": 2}}, {"$project": {"a": 1}}])

    assert pipeline == [
        {"$match": {"usertype": 2}},
        {"$limit": 50},
        {"$project": {"a": 1}},
        {"$limit": 50},
    ]


def test_limit_is_never_pushed_ahead_of_the_forced_domain_match():
    """scope_spec_to_domain prepends the usertype predicate. A limit ahead of it would take 50
    arbitrary docs from the whole shared `users` collection and only then filter by domain --
    returning a near-empty, under-scoped result."""
    pipeline = _pipeline_for([{"$match": {"usertype": 2}}, {"$project": {"a": 1}}])

    assert pipeline[0] == {"$match": {"usertype": 2}}


def test_an_aggregating_stage_blocks_the_pushdown():
    """Limiting the input to a $group would change "sum over all orders" into "sum over an
    arbitrary 50" -- a wrong answer that looks entirely plausible."""
    stages = [{"$match": {"usertype": 2}}, {"$group": {"_id": "$v", "n": {"$sum": 1}}}]

    assert _pipeline_for(stages) == [*stages, {"$limit": 50}]


def test_a_later_match_blocks_the_pushdown():
    """$match looks harmless but changes cardinality: limiting before it yields 50 documents
    that are then filtered down to fewer, where the original yields 50 *matching* ones."""
    stages = [{"$match": {"usertype": 2}}, {"$project": {"a": 1}}, {"$match": {"a": 1}}]

    assert _pipeline_for(stages) == [*stages, {"$limit": 50}]


def test_an_unwind_blocks_the_pushdown():
    """$unwind is one-to-many, so limiting before it under-counts the output rows."""
    stages = [{"$unwind": "$items"}]

    assert _pipeline_for(stages) == [*stages, {"$limit": 50}]


def test_a_pipeline_of_only_one_to_one_stages_is_limited_first():
    assert _pipeline_for([{"$project": {"a": 1}}]) == [
        {"$limit": 50},
        {"$project": {"a": 1}},
        {"$limit": 50},
    ]


def test_an_empty_pipeline_gets_a_single_limit():
    """A second identical limit would be pure noise in the audit log."""
    assert _pipeline_for([]) == [{"$limit": 50}]


def test_geo_near_stays_the_first_stage_when_a_limit_is_pushed_down():
    """$geoNear must lead an aggregation; a pushed-down limit must not displace it."""
    collection = _FakeCollection([{"a": 1}])
    db = _FakeDb(users=collection)
    spec = QuerySpec(
        collection="users",
        operation="aggregate",
        pipeline=[{"$match": {"usertype": 2}}, {"$project": {"a": 1}}],
        geo_near=GeoNear(field="location", longitude=67.0, latitude=24.8, max_distance_m=5000),
        limit=50,
    )

    execute_query_spec(db, spec)

    assert "$geoNear" in collection.aggregate_calls[0][0]


# --- count ---------------------------------------------------------------------------------


def test_an_unfiltered_count_uses_collection_metadata():
    """count_documents on no filter is a full scan purely to produce a number the collection's
    own metadata already holds."""
    collection = _FakeCollection([{"a": 1}])
    db = _FakeDb(orders=collection)

    rows = execute_query_spec(db, QuerySpec(collection="orders", operation="count"))

    assert rows == [{"count": 9999}]
    assert collection.estimated_calls and not collection.count_calls


def test_a_filtered_count_must_actually_count():
    """estimated_document_count ignores filters, so it would silently answer a different
    question than the one asked."""
    collection = _FakeCollection([{"a": 1}, {"a": 2}])
    db = _FakeDb(orders=collection)
    spec = QuerySpec(collection="orders", operation="count", filter={"status": "pending"})

    rows = execute_query_spec(db, spec)

    assert rows == [{"count": 2}]
    assert collection.count_calls == [{"status": "pending"}]
    assert not collection.estimated_calls


def test_an_unsupported_operation_is_rejected():
    db = _FakeDb(orders=_FakeCollection())
    spec = QuerySpec(collection="orders", operation="find")
    object.__setattr__(spec, "operation", "drop")

    with pytest.raises(ValueError, match="Unsupported operation"):
        execute_query_spec(db, spec)


# --- date coercion -------------------------------------------------------------------------
#
# MongoDB compares a string against a stored datetime as unequal, always -- so a date filter
# that isn't coerced silently returns zero rows rather than erroring.


def test_iso_datetime_strings_in_a_filter_become_real_datetimes():
    collection = _FakeCollection([])
    db = _FakeDb(orders=collection)
    spec = QuerySpec(
        collection="orders",
        operation="find",
        filter={"created_at": {"$gte": "2026-09-01T00:00:00Z"}},
    )

    execute_query_spec(db, spec)

    coerced = collection.find_calls[0]["created_at"]["$gte"]
    assert isinstance(coerced, datetime)
    assert coerced.tzinfo is not None


def test_a_datetime_without_an_offset_is_treated_as_utc():
    collection = _FakeCollection([])
    db = _FakeDb(orders=collection)
    spec = QuerySpec(
        collection="orders", operation="find", filter={"created_at": {"$lt": "2026-09-01T12:00:00"}}
    )

    execute_query_spec(db, spec)

    assert collection.find_calls[0]["created_at"]["$lt"].tzinfo is not None


def test_a_plain_date_string_is_left_alone():
    """Only strings containing 'T' are candidates, so a date-only value stays a string."""
    collection = _FakeCollection([])
    db = _FakeDb(orders=collection)

    execute_query_spec(
        db, QuerySpec(collection="orders", operation="find", filter={"day": "2026-09-01"})
    )

    assert collection.find_calls[0]["day"] == "2026-09-01"


def test_a_non_date_string_containing_t_survives_unchanged():
    """ "T" is a cheap prefilter, not proof of a date -- an unparseable value must pass through
    rather than raise."""
    collection = _FakeCollection([])
    db = _FakeDb(orders=collection)

    execute_query_spec(
        db, QuerySpec(collection="orders", operation="find", filter={"note": "NOT A DATE"})
    )

    assert collection.find_calls[0]["note"] == "NOT A DATE"
