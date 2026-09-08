"""Run a validated QuerySpec against MongoDB and return JSON-safe, policy-sanitized results.

This module is the single choke point where database rows enter the application, which makes it
the right and only place to apply app/security/field_policy.py. Everything downstream -- the
LLM answer prompt, CSV/XLSX/PDF exports, the answer cache, the audit log -- consumes what this
function returns, so sanitizing here covers every path by construction, including paths added
later. Sanitizing further downstream (as a pass over a finished answer) was the earlier shape
and it leaked: the model had already been handed raw `_id`s and card numbers by then.
"""

from datetime import datetime, timezone
from typing import Any

from bson import ObjectId
from pymongo.database import Database

from app.rag.query_spec import GeoNear, QuerySpec
from app.security.field_policy import sanitize_rows

# Stages that reshape each document independently, one in and one out, and neither select nor
# reorder. Only these are safe to have *after* a pushed-down $limit: taking n documents and then
# projecting them yields the same n documents as projecting everything and then taking n.
#
# The list is deliberately an allowlist rather than a denylist of "aggregating" stages. $match
# and $unwind look harmless but both change cardinality -- limiting before a later $match yields
# n documents that are then filtered down to fewer, where the original yields n *matching* ones.
# Getting that backwards silently returns too little data, so anything not proven 1:1 is excluded.
_ONE_TO_ONE_STAGES = frozenset(
    {"$project", "$addFields", "$set", "$unset", "$replaceRoot", "$replaceWith"}
)


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, (ObjectId, datetime)):
        return str(value)
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    return value


_ISO_SUFFIXES = ("Z", "+00:00")


def _try_parse_iso_datetime(value: str) -> datetime | str:
    """Return a timezone-aware datetime if `value` looks like an ISO-8601 datetime,
    otherwise return it unchanged.  Only called on strings that already contain 'T',
    so plain date strings like "2026-09-01" and non-date strings are never touched."""
    # Normalise the trailing Z -> +00:00 so fromisoformat() accepts it (Python < 3.11
    # doesn't handle the trailing Z).
    normalised = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(normalised)
        # Attach UTC if the string had no offset (shouldn't happen with our prompts, but be safe).
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return value


def _coerce_dates(obj: Any) -> Any:
    """Recursively walk a filter or pipeline structure and replace ISO-8601 datetime
    strings with proper datetime objects so MongoDB compares them correctly against
    stored datetime fields (string vs datetime comparison always yields no matches)."""
    if isinstance(obj, str) and "T" in obj:
        return _try_parse_iso_datetime(obj)
    if isinstance(obj, dict):
        return {k: _coerce_dates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_coerce_dates(item) for item in obj]
    return obj


def _near_filter_clause(geo_near: GeoNear) -> dict:
    return {
        geo_near.field: {
            "$near": {
                "$geometry": {
                    "type": "Point",
                    "coordinates": [geo_near.longitude, geo_near.latitude],
                },
                "$maxDistance": geo_near.max_distance_m,
            }
        }
    }


def _geo_near_stage(geo_near: GeoNear) -> dict:
    # $geoNear must be the first stage of an aggregation pipeline -- callers insert this ahead
    # of everything else, including a usertype $match prepended by scope_spec_to_domain
    # (app/agents/domains.py), which is a valid ordering ($match may follow $geoNear).
    return {
        "$geoNear": {
            "near": {"type": "Point", "coordinates": [geo_near.longitude, geo_near.latitude]},
            "distanceField": "distance_m",
            "maxDistance": geo_near.max_distance_m,
            "spherical": True,
            "key": geo_near.field,
        }
    }


def _build_pipeline(spec: QuerySpec) -> list[dict]:
    """Assemble the aggregation pipeline, pushing the row limit down when that is provably safe.

    Appending `{"$limit": n}` only at the end bounds how much comes back over the wire but not
    how much the server reads: a `$group` still scans the entire collection first.

    The limit can be pushed down only past the *leading run of `$match` stages* -- and only when
    every stage after that run is 1:1 (see _ONE_TO_ONE_STAGES). Two constraints drive this:

    * It must land after the leading `$match`, never before. `scope_spec_to_domain` prepends the
      forced `usertype` predicate as the first stage, so a limit ahead of it would take n
      documents from the whole shared `users` collection and only then filter by domain --
      returning a near-empty, under-scoped result.
    * Everything after it must preserve cardinality, or the limit silently truncates the input
      to a stage that needed all of it.

    When either constraint fails, the trailing limit is all that can be applied without changing
    the answer. Correctness wins over speed there.
    """
    stages = list(spec.pipeline)
    trailing_limit = {"$limit": spec.limit}

    leading_matches = 0
    for stage in stages:
        if set(stage) == {"$match"}:
            leading_matches += 1
        else:
            break

    rest = stages[leading_matches:]
    if not all(set(stage) <= _ONE_TO_ONE_STAGES for stage in rest):
        return [*stages, trailing_limit]
    if not rest:
        # Nothing follows the pushed-down limit, so a second identical one is pure noise.
        return [*stages, trailing_limit]

    return [*stages[:leading_matches], trailing_limit, *rest, trailing_limit]


def execute_query_spec(db: Database, spec: QuerySpec, timeout_ms: int = 5000) -> list[dict]:
    """Execute a *validated* spec and return sanitized, JSON-safe rows.

    Every return path goes through sanitize_rows() -- see this module's docstring for why that
    belongs here rather than closer to the user.
    """
    collection = db[spec.collection]

    if spec.operation == "count":
        filter_ = _coerce_dates(spec.filter)
        if spec.geo_near is not None:
            filter_ = {**filter_, **_near_filter_clause(spec.geo_near)}
        if not filter_:
            # An unfiltered count_documents() is a full collection scan purely to produce a
            # number the collection's own metadata already holds.
            count = collection.estimated_document_count(maxTimeMS=timeout_ms)
        else:
            count = collection.count_documents(filter_, maxTimeMS=timeout_ms)
        return [{"count": count}]

    if spec.operation == "find":
        filter_ = _coerce_dates(spec.filter)
        if spec.geo_near is not None:
            filter_ = {**filter_, **_near_filter_clause(spec.geo_near)}
        cursor = (
            collection.find(
                filter_,
                spec.projection,
                sort=list(spec.sort.items()) if spec.sort else None,
            )
            .limit(spec.limit)
            .max_time_ms(timeout_ms)
        )
        return sanitize_rows([_to_jsonable(doc) for doc in cursor])

    if spec.operation == "aggregate":
        pipeline = _coerce_dates(_build_pipeline(spec))
        if spec.geo_near is not None:
            pipeline = [_geo_near_stage(spec.geo_near), *pipeline]
        cursor = collection.aggregate(pipeline, maxTimeMS=timeout_ms)
        return sanitize_rows([_to_jsonable(doc) for doc in cursor])

    raise ValueError(f"Unsupported operation: {spec.operation}")
