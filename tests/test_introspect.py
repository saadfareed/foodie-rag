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


def test_load_existing_summary_empty_file_returns_empty_instead_of_raising(tmp_path):
    path = tmp_path / "schema_summary.json"
    path.write_text("")  # e.g. a truncated/interrupted previous run

    assert load_existing_summary(str(path)) == {}


# --- the CLI entry point --------------------------------------------------------------------
#
# introspect is offline tooling, but its output *is* the model's entire understanding of the
# data (schema_summary.json). A bug here shows up as bad queries, not as a failed command.


class _MultiCollectionDb(dict):
    """A db whose collections are built on demand from a {name: docs} mapping.

    Named distinctly from the module-level `_FakeDb` above rather than reusing it: that one is
    constructed with pre-built collections, and shadowing it here silently broke the test that
    depends on it.
    """

    def __init__(self, collections):
        super().__init__()
        self._collections = collections

    def __getitem__(self, name):
        return _FakeCollection(self._collections.get(name, []))

    def list_collection_names(self):
        return list(self._collections)


def _run_main(monkeypatch, tmp_path, argv, collections):
    import sys

    from app.db import introspect

    out = tmp_path / "schema_summary.json"
    monkeypatch.setattr(introspect, "get_db", lambda: _MultiCollectionDb(collections))
    monkeypatch.setattr(sys, "argv", ["introspect", "--out", str(out), *argv])
    introspect.main()
    return json.loads(out.read_text())


def test_main_writes_a_summary_for_every_collection(monkeypatch, tmp_path, capsys):
    summary = _run_main(
        monkeypatch, tmp_path, [], {"orders": [{"amount": 1.5}], "users": [{"name": "Ayesha"}]}
    )

    assert {entry["collection"] for entry in summary} == {"orders", "users"}
    assert "Review this file" in capsys.readouterr().out


def test_main_can_profile_a_named_subset(monkeypatch, tmp_path, capsys):
    summary = _run_main(
        monkeypatch,
        tmp_path,
        ["--collections", "orders"],
        {"orders": [{"amount": 1.5}], "users": [{"name": "Ayesha"}]},
    )
    capsys.readouterr()

    assert [entry["collection"] for entry in summary] == ["orders"]


def test_profiling_a_subset_does_not_drop_the_other_collections(monkeypatch, tmp_path, capsys):
    """Re-profiling one collection must merge into the existing file. Overwriting it would
    silently blind the model to every other collection until someone re-ran the whole thing."""
    out = tmp_path / "schema_summary.json"
    out.write_text(
        json.dumps([{"collection": "users", "sampled_documents": 1, "fields": {"name": {}}}])
    )
    import sys

    from app.db import introspect

    monkeypatch.setattr(
        introspect, "get_db", lambda: _MultiCollectionDb({"orders": [{"amount": 1.5}]})
    )
    monkeypatch.setattr(sys, "argv", ["introspect", "--out", str(out), "--collections", "orders"])
    introspect.main()
    capsys.readouterr()

    summary = json.loads(out.read_text())
    assert {entry["collection"] for entry in summary} == {"orders", "users"}
