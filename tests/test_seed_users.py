from app.db.seed_users import (
    CITIES,
    LOYALTY_TIERS,
    VENDOR_CATEGORIES,
    generate_sample_users,
    seed_users,
)


def test_generate_sample_users_counts():
    users = generate_sample_users(customers=5, vendors=3)
    assert sum(1 for u in users if u["usertype"] == 1) == 5
    assert sum(1 for u in users if u["usertype"] == 2) == 3


def test_generate_sample_users_ids_are_unique():
    users = generate_sample_users(customers=10, vendors=10)
    ids = [u["user_id"] for u in users]
    assert len(ids) == len(set(ids))


def test_customers_have_loyalty_tier_not_vendor_fields():
    users = generate_sample_users(customers=5, vendors=0)
    for user in users:
        assert user["loyalty_tier"] in LOYALTY_TIERS
        assert "business_name" not in user
        assert "category" not in user
        assert "rating" not in user


def test_vendors_have_business_fields_not_loyalty_tier():
    users = generate_sample_users(customers=0, vendors=5)
    for user in users:
        assert user["category"] in VENDOR_CATEGORIES
        assert user["business_name"]
        assert 3.0 <= user["rating"] <= 5.0
        assert "loyalty_tier" not in user


def test_locations_are_scattered_near_a_known_city():
    users = generate_sample_users(customers=20, vendors=0)
    city_centers = {name: (lng, lat) for name, lng, lat in CITIES}
    for user in users:
        lng, lat = user["location"]["coordinates"]
        center_lng, center_lat = city_centers[user["city"]]
        assert abs(lng - center_lng) <= 0.15
        assert abs(lat - center_lat) <= 0.15


def test_last_active_at_never_precedes_created_at_or_exceeds_now():
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    users = generate_sample_users(customers=20, vendors=20)
    for user in users:
        assert user["created_at"] <= user["last_active_at"] <= now


class _FakeCollection:
    def __init__(self):
        self.deleted = False
        self.inserted: list = []
        self.indexes_created: list = []

    def delete_many(self, filter_):
        self.deleted = True

    def insert_many(self, docs):
        self.inserted = list(docs)

        class _Result:
            inserted_ids = list(range(len(docs)))

        return _Result()

    def create_index(self, keys, **kwargs):
        self.indexes_created.append(keys)


class _FakeDb(dict):
    def __getitem__(self, name):
        return dict.setdefault(self, name, _FakeCollection())


def test_seed_users_inserts_and_ensures_indexes(monkeypatch):
    fake_db = _FakeDb()
    monkeypatch.setattr("app.db.seed_users.get_db", lambda: fake_db)

    inserted = seed_users(customers=3, vendors=2)

    assert inserted == 5
    assert len(fake_db["users"].inserted) == 5
    assert fake_db["users"].deleted is True
    assert [("location", "2dsphere")] in fake_db["users"].indexes_created


def test_seed_users_keep_existing_skips_delete(monkeypatch):
    fake_db = _FakeDb()
    monkeypatch.setattr("app.db.seed_users.get_db", lambda: fake_db)

    seed_users(customers=1, vendors=1, clear_existing=False)

    assert fake_db["users"].deleted is False
