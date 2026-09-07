"""Turn schema_summary.json (+ optional human annotations) into prompt text.

Every field name that reaches a domain agent's prompt comes from here, which makes this the
place to enforce the *first* half of the field policy: internal storage plumbing (`_id`, `__v`,
index fields) is filtered out before the model ever sees the schema. A name the model was never
shown is a name it cannot project, sort by, or reference -- which is a stronger guarantee than
stripping the field from the result afterwards, and it composes with the two layers that follow
(app/rag/validator.py rejects a spec naming a denied field anyway; app/db/executor.py sanitizes
whatever comes back).

Note this also shrinks the prompt, since these fields were pure noise in it to begin with.
"""

import json
import logging
import os
import threading

from app.security.field_policy import is_internal_field, is_secret_field

logger = logging.getLogger("audit")

# Keyed by (summary_path, annotations_path) -> (summary_mtime, annotations_mtime, summary, notes).
# Both files are static prompt inputs re-read on every question; caching avoids redundant disk
# I/O + JSON parsing per request while still picking up edits (e.g. a re-run of introspect.py or
# an updated schema_annotations.json) via mtime comparison, no restart required.
_cache: dict[tuple[str, str], tuple[float | None, float | None, list, dict]] = {}

# Rendered prompt text, keyed by (collection, fields, summary_path, annotations_path). Rendering
# is pure string work over already-parsed JSON, but it ran on every domain agent of every
# question -- for a static input. Memoized behind the same mtime check as _cache.
_rendered_cache: dict[tuple, str] = {}

_cache_lock = threading.Lock()


def _mtime(path: str) -> float | None:
    return os.path.getmtime(path) if os.path.exists(path) else None


def _current_mtimes(summary_path: str, annotations_path: str) -> tuple[float | None, float | None]:
    """mtimes for both files.

    The stat() calls happen on every lookup rather than being throttled behind a timer. Skipping
    them would save microseconds, and would cost the documented guarantee that re-running
    introspect.py is picked up without a restart -- for a saving that is invisible next to the
    JSON parse it guards, let alone the Gemini call downstream of it.
    """
    return (_mtime(summary_path), _mtime(annotations_path))


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
    current_mtimes = _current_mtimes(summary_path, annotations_path)

    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached is not None and cached[:2] == current_mtimes:
            return cached[2], cached[3]

    summary = _load_json(summary_path) or []
    annotations = _load_json(annotations_path) or {}

    with _cache_lock:
        stale_keys = [k for k in _rendered_cache if k[2:] == cache_key]
        for k in stale_keys:
            del _rendered_cache[k]
        _cache[cache_key] = (*current_mtimes, summary, annotations)
    return summary, annotations


def _is_exposable_field(field: str) -> bool:
    """Whether a field from schema_summary.json may appear in a prompt at all.

    Internal fields are hidden outright. Secret-valued fields are hidden too: the model has no
    reason to filter or project a card number, and the executor would only hand it back
    redacted, so showing it invites a query that returns a column of placeholders.
    """
    return not is_internal_field(field) and not is_secret_field(field)


def _render_collection(collection: dict, annotations: dict, fields: list[str] | None) -> list[str]:
    name = collection["collection"]
    lines = [f"Collection: {name}"]
    for field, info in collection.get("fields", {}).items():
        if fields is not None and field not in fields:
            continue
        if not _is_exposable_field(field):
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

    render_key = (
        collection,
        tuple(fields) if fields is not None else None,
        summary_path,
        annotations_path,
    )
    with _cache_lock:
        rendered = _rendered_cache.get(render_key)
    if rendered is not None:
        return rendered

    match = next((c for c in summary if c["collection"] == collection), None)
    if match is None:
        return f"No schema information is available yet for '{collection}'."

    result = "\n".join(_render_collection(match, annotations, fields)).strip()
    with _cache_lock:
        _rendered_cache[render_key] = result
    return result
