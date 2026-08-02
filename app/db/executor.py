"""Run a validated QuerySpec against MongoDB and return JSON-safe results."""

from datetime import datetime
from typing import Any

from bson import ObjectId
from pymongo.database import Database

from app.rag.query_spec import QuerySpec


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, (ObjectId, datetime)):
        return str(value)
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    return value


def execute_query_spec(db: Database, spec: QuerySpec, timeout_ms: int = 5000) -> list[dict]:
    collection = db[spec.collection]

    if spec.operation == "count":
        return [{"count": collection.count_documents(spec.filter, maxTimeMS=timeout_ms)}]

    if spec.operation == "find":
        cursor = (
            collection.find(
                spec.filter,
                spec.projection,
                sort=list(spec.sort.items()) if spec.sort else None,
            )
            .limit(spec.limit)
            .max_time_ms(timeout_ms)
        )
        return [_to_jsonable(doc) for doc in cursor]

    if spec.operation == "aggregate":
        pipeline = [*spec.pipeline, {"$limit": spec.limit}]
        cursor = collection.aggregate(pipeline, maxTimeMS=timeout_ms)
        return [_to_jsonable(doc) for doc in cursor]

    raise ValueError(f"Unsupported operation: {spec.operation}")
