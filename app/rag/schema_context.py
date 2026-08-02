"""Turn schema_summary.json (+ optional human annotations) into prompt text."""

import json
import logging
import os

logger = logging.getLogger("audit")


def _load_json(path: str) -> object | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except json.JSONDecodeError:
        logger.warning("Ignoring unreadable/malformed JSON file: %s", path)
        return None


def build_schema_context(
    summary_path: str = "schema_summary.json",
    annotations_path: str = "schema_annotations.json",
) -> str:
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
