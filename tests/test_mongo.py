import pytest

from app.db import mongo

# This module's subject *is* get_client, and it stubs the driver a layer lower (MongoClient
# itself), so the conftest tripwire that refuses real client construction would replace the
# very function under test.
pytestmark = pytest.mark.uses_mongo_client


class _FakeMongoClient:
    instances = []

    def __init__(self, uri, **kwargs):
        self.uri = uri
        self.kwargs = kwargs
        self.closed = False
        _FakeMongoClient.instances.append(self)

    def __getitem__(self, name):
        return name

    def close(self):
        self.closed = True


def _reset():
    mongo._client = None
    _FakeMongoClient.instances.clear()


def test_get_client_applies_configured_timeouts_and_pool_size(monkeypatch):
    _reset()
    monkeypatch.setattr(mongo, "MongoClient", _FakeMongoClient)
    monkeypatch.setattr(mongo.settings, "mongodb_uri", "mongodb://test")
    monkeypatch.setattr(mongo.settings, "mongodb_server_selection_timeout_ms", 1111)
    monkeypatch.setattr(mongo.settings, "mongodb_connect_timeout_ms", 2222)
    monkeypatch.setattr(mongo.settings, "mongodb_socket_timeout_ms", 3333)
    monkeypatch.setattr(mongo.settings, "mongodb_max_pool_size", 7)

    client = mongo.get_client()

    assert client.uri == "mongodb://test"
    assert client.kwargs == {
        "serverSelectionTimeoutMS": 1111,
        "connectTimeoutMS": 2222,
        "socketTimeoutMS": 3333,
        "maxPoolSize": 7,
    }


def test_get_client_is_a_singleton(monkeypatch):
    _reset()
    monkeypatch.setattr(mongo, "MongoClient", _FakeMongoClient)

    first = mongo.get_client()
    second = mongo.get_client()

    assert first is second
    assert len(_FakeMongoClient.instances) == 1


def test_close_client_closes_and_clears_the_singleton(monkeypatch):
    _reset()
    monkeypatch.setattr(mongo, "MongoClient", _FakeMongoClient)

    client = mongo.get_client()
    mongo.close_client()

    assert client.closed is True
    assert mongo._client is None


def test_close_client_is_a_safe_no_op_when_never_created(monkeypatch):
    _reset()
    mongo.close_client()  # must not raise
    assert mongo._client is None
