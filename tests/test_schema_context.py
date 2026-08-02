import json

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
