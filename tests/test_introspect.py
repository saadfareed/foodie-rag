import json
from datetime import datetime

from bson import ObjectId

from app.db.introspect import _example, _type_name, load_existing_summary, profile_collection


def test_type_name_for_bson_and_python_types():
    assert _type_name(ObjectId()) == "ObjectId"
    assert _type_name(datetime.now()) == "datetime"
    assert _type_name(True) == "bool"
    assert _type_name([1, 2]) == "array"
    assert _type_name({"a": 1}) == "object"
    assert _type_name(1.5) == "float"
    assert _type_name("x") == "str"


def test_example_shortens_containers():
    assert _example({"a": 1}) == "{...}"
    assert _example([1, 2, 3]) == "[3 items]"
    assert _example(5) == 5


class _FakeCursor(list):
    def limit(self, n):
        return self[:n]


class _FakeCollection:
    def __init__(self, docs):
        self._docs = docs

    def find(self):
        return _FakeCursor(self._docs)


class _FakeDb(dict):
    def __getitem__(self, name):
        return dict.__getitem__(self, name)


def test_profile_collection_aggregates_types_and_examples():
    docs = [
        {"_id": ObjectId(), "amount": 10, "status": "shipped"},
        {"_id": ObjectId(), "amount": 20.5, "status": "pending"},
    ]
    db = _FakeDb(orders=_FakeCollection(docs))

    result = profile_collection(db, "orders", sample_size=10)

    assert result["collection"] == "orders"
    assert result["sampled_documents"] == 2
    assert set(result["fields"]["amount"]["types"]) == {"int", "float"}
    assert "shipped" in result["fields"]["status"]["examples"]


def test_load_existing_summary_missing_file_returns_empty(tmp_path):
    assert load_existing_summary(str(tmp_path / "missing.json")) == {}


def test_load_existing_summary_keys_by_collection_name(tmp_path):
    path = tmp_path / "schema_summary.json"
    path.write_text(
        json.dumps(
            [
                {"collection": "orders", "sampled_documents": 5, "fields": {}},
                {"collection": "customers", "sampled_documents": 3, "fields": {}},
            ]
        )
    )

    result = load_existing_summary(str(path))

    assert set(result) == {"orders", "customers"}
    assert result["orders"]["sampled_documents"] == 5
