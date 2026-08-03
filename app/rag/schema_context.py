"""Turn schema_summary.json (+ optional human annotations) into prompt text."""

import json
import logging
import os

logger = logging.getLogger("audit")

# Keyed by (summary_path, annotations_path) -> (summary_mtime, annotations_mtime, summary, notes).
# Both files are static prompt inputs re-read on every question; caching avoids redundant disk
# I/O + JSON parsing per request while still picking up edits (e.g. a re-run of introspect.py or
# an updated schema_annotations.json) via mtime comparison, no restart required.
_cache: dict[tuple[str, str], tuple[float | None, float | None, list, dict]] = {}


def _mtime(path: str) -> float | None:
    return os.path.getmtime(path) if os.path.exists(path) else None


def _load_json(path: str) -> object | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except json.JSONDecodeError:
        logger.warning("Ignoring unreadable/malformed JSON file: %s", path)
        return None


def _load_schema_data(summary_path: str, annotations_path: str) -> tuple[list, dict]:
    cache_key = (summary_path, annotations_path)
    current_mtimes = (_mtime(summary_path), _mtime(annotations_path))

    cached = _cache.get(cache_key)
    if cached is not None and cached[:2] == current_mtimes:
        return cached[2], cached[3]

    summary = _load_json(summary_path) or []
    annotations = _load_json(annotations_path) or {}
    _cache[cache_key] = (*current_mtimes, summary, annotations)
    return summary, annotations


def _render_collection(collection: dict, annotations: dict, fields: list[str] | None) -> list[str]:
    name = collection["collection"]
    lines = [f"Collection: {name}"]
    for field, info in collection.get("fields", {}).items():
        if fields is not None and field not in fields:
            continue
        types = ", ".join(info.get("types", []))
        examples = info.get("examples", [])
        note = annotations.get(name, {}).get(field)
        line = f"  - {field}: {types} (examples: {examples})"
        if note:
            line += f" — {note}"
        lines.append(line)
    lines.append("")
    return lines


def build_schema_context(
    summary_path: str = "schema_summary.json",
    annotations_path: str = "schema_annotations.json",
) -> str:
    """Full schema context across every collection in the summary -- kept for tooling/tests;
    the live agent pipeline uses build_domain_schema_context() below so each domain agent only
    ever sees its own collection's (and, for a shared collection, its own domain's) fields."""
    summary, annotations = _load_schema_data(summary_path, annotations_path)
    if not summary:
        return "No schema information is available yet."

    lines: list[str] = []
    for collection in summary:
        lines.extend(_render_collection(collection, annotations, fields=None))
    return "\n".join(lines).strip()


def build_domain_schema_context(
    collection: str,
    fields: list[str] | None = None,
    summary_path: str = "schema_summary.json",
    annotations_path: str = "schema_annotations.json",
) -> str:
    """Schema context scoped to a single collection, optionally further scoped to a subset of
    its fields (used for `customers`/`vendors`, which share the `users` collection but should
    never see each other's exclusive fields -- see app/agents/domains.py). Keeping the prompt
    scoped this way is a hallucination guardrail in itself: a customer-domain agent that's never
    shown `rating`/`business_name` has no way to accidentally reference them."""
    summary, annotations = _load_schema_data(summary_path, annotations_path)
    match = next((c for c in summary if c["collection"] == collection), None)
    if match is None:
        return f"No schema information is available yet for '{collection}'."

    lines = _render_collection(match, annotations, fields)
    return "\n".join(lines).strip()
