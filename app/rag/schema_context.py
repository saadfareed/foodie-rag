"""Turn schema_summary.json (+ optional human annotations) into prompt text."""

import json
import logging
import os

logger = logging.getLogger("audit")

# Keyed by (summary_path, annotations_path) -> (summary_mtime, annotations_mtime, result).
# Both files are static prompt inputs re-read on every question; caching avoids redundant disk
# I/O + JSON parsing per request while still picking up edits (e.g. a re-run of introspect.py or
# an updated schema_annotations.json) via mtime comparison, no restart required.
_cache: dict[tuple[str, str], tuple[float | None, float | None, str]] = {}


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


def _render(summary_path: str, annotations_path: str) -> str:
    summary = _load_json(summary_path)
    if not summary:
        return "No schema information is available yet."

    annotations = _load_json(annotations_path) or {}

    lines: list[str] = []
    for collection in summary:
        name = collection["collection"]
        lines.append(f"Collection: {name}")
        for field, info in collection.get("fields", {}).items():
            types = ", ".join(info.get("types", []))
            examples = info.get("examples", [])
            note = annotations.get(name, {}).get(field)
            line = f"  - {field}: {types} (examples: {examples})"
            if note:
                line += f" — {note}"
            lines.append(line)
        lines.append("")

    return "\n".join(lines).strip()


def build_schema_context(
    summary_path: str = "schema_summary.json",
    annotations_path: str = "schema_annotations.json",
) -> str:
    cache_key = (summary_path, annotations_path)
    current_mtimes = (_mtime(summary_path), _mtime(annotations_path))

    cached = _cache.get(cache_key)
    if cached is not None and cached[:2] == current_mtimes:
        return cached[2]

    result = _render(summary_path, annotations_path)
    _cache[cache_key] = (*current_mtimes, result)
    return result
