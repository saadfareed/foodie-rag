import json
import os

from app.rag import schema_context
from app.rag.schema_context import build_schema_context

SUMMARY = [
    {
        "collection": "orders",
        "sampled_documents": 2,
        "fields": {
            "total_amount": {"types": ["float"], "examples": [10.5, 20.0]},
            "status": {"types": ["str"], "examples": ["shipped", "pending"]},
        },
    }
]


def test_missing_files_return_placeholder(tmp_path):
    context = build_schema_context(
        summary_path=str(tmp_path / "missing.json"),
        annotations_path=str(tmp_path / "missing_annotations.json"),
    )
    assert "No schema information" in context


def test_summary_fields_appear_in_context(tmp_path):
    summary_path = tmp_path / "schema_summary.json"
    summary_path.write_text(json.dumps(SUMMARY))

    context = build_schema_context(
        summary_path=str(summary_path),
        annotations_path=str(tmp_path / "missing_annotations.json"),
    )

    assert "Collection: orders" in context
    assert "total_amount" in context
    assert "status" in context


def test_annotations_are_merged_into_context(tmp_path):
    summary_path = tmp_path / "schema_summary.json"
    summary_path.write_text(json.dumps(SUMMARY))

    annotations_path = tmp_path / "schema_annotations.json"
    annotations_path.write_text(json.dumps({"orders": {"total_amount": "order total in USD"}}))

    context = build_schema_context(
        summary_path=str(summary_path), annotations_path=str(annotations_path)
    )

    assert "order total in USD" in context


def test_empty_summary_file_degrades_to_placeholder_instead_of_raising(tmp_path):
    summary_path = tmp_path / "schema_summary.json"
    summary_path.write_text("")  # e.g. a truncated/interrupted introspection run

    context = build_schema_context(
        summary_path=str(summary_path),
        annotations_path=str(tmp_path / "missing_annotations.json"),
    )

    assert "No schema information" in context


def test_malformed_annotations_file_is_ignored_not_fatal(tmp_path):
    summary_path = tmp_path / "schema_summary.json"
    summary_path.write_text(json.dumps(SUMMARY))

    annotations_path = tmp_path / "schema_annotations.json"
    annotations_path.write_text("{not valid json")

    context = build_schema_context(
        summary_path=str(summary_path), annotations_path=str(annotations_path)
    )

    assert "Collection: orders" in context


def test_unchanged_files_are_served_from_cache_without_reparsing(tmp_path, monkeypatch):
    summary_path = tmp_path / "schema_summary.json"
    summary_path.write_text(json.dumps(SUMMARY))
    annotations_path = tmp_path / "missing_annotations.json"

    schema_context._cache.clear()
    first = build_schema_context(
        summary_path=str(summary_path), annotations_path=str(annotations_path)
    )

    def _boom(_path):
        raise AssertionError("_load_json should not be called again for unchanged files")

    monkeypatch.setattr(schema_context, "_load_json", _boom)

    second = build_schema_context(
        summary_path=str(summary_path), annotations_path=str(annotations_path)
    )

    assert second == first


def test_editing_summary_file_invalidates_the_cache(tmp_path):
    summary_path = tmp_path / "schema_summary.json"
    summary_path.write_text(json.dumps(SUMMARY))
    annotations_path = tmp_path / "missing_annotations.json"

    schema_context._cache.clear()
    first = build_schema_context(
        summary_path=str(summary_path), annotations_path=str(annotations_path)
    )
    assert "status" in first

    updated = [{**SUMMARY[0], "fields": {"new_field": {"types": ["str"], "examples": ["x"]}}}]
    summary_path.write_text(json.dumps(updated))
    # Force the mtime forward in case the filesystem's clock resolution makes the two writes
    # land in the same second, which would otherwise make the cache-invalidation check flaky.
    future = os.path.getmtime(summary_path) + 5
    os.utime(summary_path, (future, future))

    second = build_schema_context(
        summary_path=str(summary_path), annotations_path=str(annotations_path)
    )

    assert "new_field" in second
    assert "status" not in second
