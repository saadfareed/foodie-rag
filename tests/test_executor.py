from datetime import datetime

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

    def find(self, filter_, projection=None, sort=None):
        self.find_calls.append(filter_)
        return _FakeCursor(self._rows)

    def aggregate(self, pipeline, maxTimeMS=None):
        self.aggregate_calls.append(pipeline)
        return _FakeCursor(self._rows)

    def count_documents(self, filter_, maxTimeMS=None):
        self.count_calls.append(filter_)
        return len(self._rows)


class _FakeDb(dict):
    def __getitem__(self, name):
        return dict.__getitem__(self, name)


def test_to_jsonable_converts_objectid_and_datetime():
    collection = _FakeCollection([{"_id": ObjectId(), "created_at": datetime(2024, 1, 1)}])
    db = _FakeDb(orders=collection)
    spec = QuerySpec(collection="orders", operation="find")

    rows = execute_query_spec(db, spec)

    assert isinstance(rows[0]["_id"], str)
    assert isinstance(rows[0]["created_at"], str)


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
