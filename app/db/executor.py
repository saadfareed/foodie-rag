"""Run a validated QuerySpec against MongoDB and return JSON-safe results."""

from datetime import datetime
from typing import Any

from bson import ObjectId
from pymongo.database import Database

from app.rag.query_spec import GeoNear, QuerySpec


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, (ObjectId, datetime)):
        return str(value)
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    return value


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


def execute_query_spec(db: Database, spec: QuerySpec, timeout_ms: int = 5000) -> list[dict]:
    collection = db[spec.collection]

    if spec.operation == "count":
        filter_ = spec.filter
        if spec.geo_near is not None:
            filter_ = {**filter_, **_near_filter_clause(spec.geo_near)}
        return [{"count": collection.count_documents(filter_, maxTimeMS=timeout_ms)}]

    if spec.operation == "find":
        filter_ = spec.filter
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
        return [_to_jsonable(doc) for doc in cursor]

    if spec.operation == "aggregate":
        pipeline = [*spec.pipeline, {"$limit": spec.limit}]
        if spec.geo_near is not None:
            pipeline = [_geo_near_stage(spec.geo_near), *pipeline]
        cursor = collection.aggregate(pipeline, maxTimeMS=timeout_ms)
        return [_to_jsonable(doc) for doc in cursor]

    raise ValueError(f"Unsupported operation: {spec.operation}")
