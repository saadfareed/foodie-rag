from app.db.indexes import ensure_indexes


class _FakeCollection:
    def __init__(self):
        self.created: list = []

    def create_index(self, keys, **kwargs):
        self.created.append(keys)


class _FakeDb(dict):
    def __getitem__(self, name):
        return dict.setdefault(self, name, _FakeCollection())


def test_ensure_indexes_covers_orders_hot_fields():
    db = _FakeDb()
    ensure_indexes(db)

    assert db["orders"].created == ["customer_id", "vendor_id", "status", "created_at"]


def test_ensure_indexes_covers_users_hot_fields_and_geo():
    db = _FakeDb()
    ensure_indexes(db)

    assert db["users"].created == ["user_id", "usertype", [("location", "2dsphere")]]


def test_ensure_indexes_is_safe_to_call_repeatedly():
    db = _FakeDb()
    ensure_indexes(db)
    ensure_indexes(db)

    assert len(db["orders"].created) == 8
    assert len(db["users"].created) == 6
