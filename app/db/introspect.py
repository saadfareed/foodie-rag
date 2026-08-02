"""Sample MongoDB collections and print a draft schema summary for user review.

Usage:
    python -m app.db.introspect [--sample-size 25] [--out schema_summary.json]
"""

import argparse
import json
from collections import defaultdict
from datetime import datetime
from typing import Any

from bson import ObjectId

from app.config import settings
from app.db.mongo import get_db


def _type_name(value: Any) -> str:
    if isinstance(value, ObjectId):
        return "ObjectId"
    if isinstance(value, datetime):
        return "datetime"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _example(value: Any) -> Any:
    if isinstance(value, (ObjectId, datetime)):
        return str(value)
    if isinstance(value, dict):
        return "{...}"
    if isinstance(value, list):
        return f"[{len(value)} items]"
    return value


def profile_collection(db, name: str, sample_size: int) -> dict:
    fields: dict[str, dict] = defaultdict(lambda: {"types": set(), "examples": []})
    docs = list(db[name].find().limit(sample_size))
    for doc in docs:
        for key, value in doc.items():
            info = fields[key]
            info["types"].add(_type_name(value))
            if len(info["examples"]) < 3:
                ex = _example(value)
                if ex not in info["examples"]:
                    info["examples"].append(ex)

    return {
        "collection": name,
        "sampled_documents": len(docs),
        "fields": {
            key: {"types": sorted(info["types"]), "examples": info["examples"]}
            for key, info in fields.items()
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-size", type=int, default=25)
    parser.add_argument("--out", default="schema_summary.json")
    args = parser.parse_args()

    db = get_db()
    collections = settings.mongodb_allowed_collections or db.list_collection_names()

    summary = [profile_collection(db, name, args.sample_size) for name in collections]

    with open(args.out, "w") as f:
        json.dump(summary, f, indent=2, default=str)

    print(f"Wrote schema summary for {len(summary)} collection(s) to {args.out}")
    print("Review this file and annotate field meanings before it's used for query generation.")


if __name__ == "__main__":
    main()
