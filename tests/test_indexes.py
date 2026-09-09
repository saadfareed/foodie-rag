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

    # Compound, equality-then-range: real questions filter on an id/status *and* a date range,
    # which two single-field indexes can only serve by intersection.
    assert db["orders"].created == [
        [("customer_id", 1), ("created_at", -1)],
        [("vendor_id", 1), ("created_at", -1)],
        [("status", 1), ("created_at", -1)],
        [("created_at", -1)],
    ]


def test_orders_compound_indexes_cover_their_single_field_prefixes():
    """A compound index serves any prefix of itself, so the standalone single-field indexes it
    subsumes are redundant -- and a redundant index still costs write throughput and memory."""
    db = _FakeDb()
    ensure_indexes(db)

    leading_fields = {keys[0][0] for keys in db["orders"].created}
    assert {"customer_id", "vendor_id", "status", "created_at"} <= leading_fields
    assert not any(isinstance(keys, str) for keys in db["orders"].created)


def test_ensure_indexes_covers_users_hot_fields_and_geo():
    db = _FakeDb()
    ensure_indexes(db)

    assert db["users"].created == [
        "user_id",
        [("usertype", 1), ("city", 1)],
        [("usertype", 1), ("status", 1)],
        [("location", "2dsphere")],
        # Sign-in looks a user up by exact email (app/db/identity.py). Unique-when-present, so
        # two accounts can't share an address -- "which identity did this person prove?" is the
        # one question authentication exists to answer.
        "email",
    ]


def test_ensure_indexes_is_safe_to_call_repeatedly():
    db = _FakeDb()
    ensure_indexes(db)
    ensure_indexes(db)

    assert len(db["orders"].created) == 8
    assert len(db["users"].created) == 10
