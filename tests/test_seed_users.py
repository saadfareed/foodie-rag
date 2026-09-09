from app.db.seed_users import (
    ADMIN_EMAIL,
    CITIES,
    DEFAULT_SEED_PASSWORD,
    LOYALTY_TIERS,
    VENDOR_CATEGORIES,
    generate_sample_users,
    seed_users,
)
from app.security.passwords import verify_password


def _people(users):
    """Customers and vendors -- everyone the demo data models as a *person in the data*.

    The seeder also emits one operator (usertype 3), which is deliberately none of the things
    these tests assert about: it has no loyalty tier, no business, and a fixed name.
    """
    return [u for u in users if u["usertype"] in (1, 2)]


def test_generate_sample_users_counts():
    users = generate_sample_users(customers=5, vendors=3)
    assert sum(1 for u in users if u["usertype"] == 1) == 5
    assert sum(1 for u in users if u["usertype"] == 2) == 3


def test_generate_sample_users_ids_are_unique():
    users = generate_sample_users(customers=10, vendors=10)
    ids = [u["user_id"] for u in users]
    assert len(ids) == len(set(ids))


def test_customers_have_loyalty_tier_not_vendor_fields():
    users = [u for u in generate_sample_users(customers=5, vendors=0) if u["usertype"] == 1]
    for user in users:
        assert user["loyalty_tier"] in LOYALTY_TIERS
        assert "business_name" not in user
        assert "category" not in user
        assert "rating" not in user


def test_vendors_have_business_fields_not_loyalty_tier():
    users = [u for u in generate_sample_users(customers=0, vendors=5) if u["usertype"] == 2]
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

    # Three customers, two vendors and the one operator account.
    assert inserted == 6
    assert len(fake_db["users"].inserted) == 6
    assert fake_db["users"].deleted is True
    assert [("location", "2dsphere")] in fake_db["users"].indexes_created


def test_seed_users_keep_existing_skips_delete(monkeypatch):
    fake_db = _FakeDb()
    monkeypatch.setattr("app.db.seed_users.get_db", lambda: fake_db)

    seed_users(customers=1, vendors=1, clear_existing=False)

    assert fake_db["users"].deleted is False


# --- real names ----------------------------------------------------------------------------


def test_users_get_real_person_names_not_placeholders():
    """A placeholder ("Customer 00007") is fine for a geo query and useless in a report -- and
    it hides formatting bugs that only appear once names vary in length."""
    from app.db.seed_users import FIRST_NAMES, LAST_NAMES

    users = _people(generate_sample_users(customers=10, vendors=5))

    for user in users:
        first, _, last = user["name"].partition(" ")
        assert first in FIRST_NAMES
        assert last in LAST_NAMES
    assert not any(u["name"].startswith(("Customer ", "Vendor ")) for u in users)


def test_names_actually_vary():
    users = _people(generate_sample_users(customers=20, vendors=10))

    assert len({u["name"] for u in users}) > 5


def test_vendor_business_names_are_not_derived_from_the_owners_name():
    """A vendor column in a report should read like a business and be visibly distinct from a
    customer name."""
    from app.db.seed_users import BUSINESS_PREFIXES

    vendors = [u for u in generate_sample_users(customers=1, vendors=10) if u["usertype"] == 2]

    for vendor in vendors:
        assert vendor["business_name"] != vendor["name"]
        assert vendor["name"] not in vendor["business_name"]
        assert vendor["business_name"].split()[0] in BUSINESS_PREFIXES


def test_a_vendors_business_name_matches_its_category():
    from app.db.seed_users import BUSINESS_SUFFIXES

    vendors = [u for u in generate_sample_users(customers=1, vendors=15) if u["usertype"] == 2]

    for vendor in vendors:
        suffixes = BUSINESS_SUFFIXES[vendor["category"]]
        assert any(vendor["business_name"].endswith(s) for s in suffixes), vendor["business_name"]


def test_customers_have_no_business_name():
    """Vendor-only fields are absent on a customer rather than null -- which is what lets the
    enrichment layer fall back from business_name to name."""
    customers = [u for u in generate_sample_users(customers=10, vendors=1) if u["usertype"] == 1]

    assert all("business_name" not in c for c in customers)


# --- signing in ------------------------------------------------------------------------------


def test_every_account_can_sign_in_with_the_seeded_password():
    """The demo is unusable if the accounts it creates can't be logged into, and "what is the
    password" is the first thing anyone asks."""
    users = generate_sample_users(customers=2, vendors=2)

    for user in users:
        assert verify_password(DEFAULT_SEED_PASSWORD, user["password_hash"])


def test_the_password_is_never_stored_as_itself():
    users = generate_sample_users(customers=2, vendors=1)

    for user in users:
        assert DEFAULT_SEED_PASSWORD not in user["password_hash"]


def test_each_account_is_hashed_with_its_own_salt():
    """One password hashed once and copied would make the demo data teach the wrong shape --
    and make one cracked hash a lookup table for every other account."""
    users = generate_sample_users(customers=3, vendors=3)

    assert len({u["password_hash"] for u in users}) == len(users)


def test_a_chosen_password_is_the_one_that_works():
    users = generate_sample_users(customers=1, vendors=0, password="something-else")

    assert verify_password("something-else", users[0]["password_hash"])
    assert not verify_password(DEFAULT_SEED_PASSWORD, users[0]["password_hash"])


# --- the operator account --------------------------------------------------------------------


def test_there_is_exactly_one_admin_account_and_it_can_sign_in():
    """Somebody has to be able to reach the admin view of the playground, and an admin whose
    status came up "suspended" would be a demo that randomly doesn't work."""
    admins = [u for u in generate_sample_users(customers=4, vendors=4) if u["usertype"] == 3]

    assert len(admins) == 1
    assert admins[0]["email"] == ADMIN_EMAIL
    assert admins[0]["status"] == "active"
    assert verify_password(DEFAULT_SEED_PASSWORD, admins[0]["password_hash"])


def test_the_admin_is_not_a_customer_or_a_vendor():
    """usertype 3 is invisible to both data domains by construction (app/agents/domains.py scopes
    them to 1 and 2), so an operator account must never carry either domain's fields."""
    admin = next(u for u in generate_sample_users(customers=2, vendors=2) if u["usertype"] == 3)

    assert "loyalty_tier" not in admin
    assert "business_name" not in admin
    assert "category" not in admin
