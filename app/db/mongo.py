from pymongo import MongoClient
from pymongo.database import Database

from app.config import settings

_client: MongoClient | None = None


def get_client() -> MongoClient:
    global _client
    if _client is None:
        _client = MongoClient(
            settings.mongodb_uri,
            serverSelectionTimeoutMS=settings.mongodb_server_selection_timeout_ms,
            connectTimeoutMS=settings.mongodb_connect_timeout_ms,
            socketTimeoutMS=settings.mongodb_socket_timeout_ms,
            maxPoolSize=settings.mongodb_max_pool_size,
        )
    return _client


def get_db() -> Database:
    return get_client()[settings.mongodb_db_name]


def close_client() -> None:
    """Close the pooled MongoClient. Safe to call more than once (e.g. from a signal handler
    and again from a shutdown finally-block)."""
    global _client
    if _client is not None:
        _client.close()
        _client = None
