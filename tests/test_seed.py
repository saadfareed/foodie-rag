import random

import pytest

from app.db.seed import (
    PAYMENT_METHOD_MAP,
    build_payby,
    build_payment_fields,
    generate_sample_orders,
    seed_orders,
)

CUSTOMER_IDS = ["USR-00001", "USR-00002"]
VENDOR_IDS = ["USR-00031", "USR-00032"]


def test_payment_method_mapping_matches_spec():
    assert build_payment_fields("cash") == (1, False)
    assert build_payment_fields("card") == (2, False)
    assert build_payment_fields("wallet") == (1, True)
    assert build_payment_fields("hybrid_card_wallet") == (2, True)
    assert build_payment_fields("hybrid_wallet_cash") == (1, True)


def test_generate_sample_orders_count_per_method():
    orders = generate_sample_orders(CUSTOMER_IDS, VENDOR_IDS, count_per_method=3)
    assert len(orders) == 3 * len(PAYMENT_METHOD_MAP)
    for method in PAYMENT_METHOD_MAP:
        assert sum(1 for o in orders if o["payment_method"] == method) == 3


def test_generate_sample_orders_fields_match_mapping():
    orders = generate_sample_orders(CUSTOMER_IDS, VENDOR_IDS, count_per_method=2)
    for order in orders:
        expected_code, expected_wallet = PAYMENT_METHOD_MAP[order["payment_method"]]
        assert order["onlinepaymentmethod"] == expected_code
        assert order["isWallet"] == expected_wallet
        assert order["amount"] > 0
        assert order["order_id"].startswith("ORD-")


def test_generate_sample_orders_ids_are_unique():
    orders = generate_sample_orders(CUSTOMER_IDS, VENDOR_IDS, count_per_method=4)
    ids = [o["order_id"] for o in orders]
    assert len(ids) == len(set(ids))


def test_generate_sample_orders_customer_and_vendor_ids_come_from_given_pools():
    orders = generate_sample_orders(CUSTOMER_IDS, VENDOR_IDS, count_per_method=4)
    for order in orders:
        assert order["customer_id"] in CUSTOMER_IDS
        assert order["vendor_id"] in VENDOR_IDS


def test_payby_single_method_equals_full_amount():
    rng = random.Random(0)
    assert build_payby("cash", 100, rng) == {"cash": 100}
    assert build_payby("card", 100, rng) == {"card": 100}
    assert build_payby("wallet", 100, rng) == {"wallet": 100}


def test_payby_hybrid_keys_match_components():
    rng = random.Random(0)
    assert set(build_payby("hybrid_wallet_cash", 100, rng)) == {"wallet", "cash"}
    assert set(build_payby("hybrid_card_wallet", 100, rng)) == {"wallet", "card"}


def test_payby_hybrid_values_sum_to_amount():
    rng = random.Random(1)
    for _ in range(20):
        amount = round(rng.uniform(5, 500), 2)
        payby = build_payby("hybrid_wallet_cash", amount, rng)
        assert round(sum(payby.values()), 2) == amount


def test_generate_sample_orders_payby_sums_to_amount():
    orders = generate_sample_orders(CUSTOMER_IDS, VENDOR_IDS, count_per_method=6)
    for order in orders:
        assert round(sum(order["payby"].values()), 2) == order["amount"]


def test_seed_orders_fails_clearly_when_users_collection_is_empty(monkeypatch):
    class _FakeCollection:
        def distinct(self, field, filter_):
            return []

    class _FakeDb(dict):
        def __getitem__(self, name):
            return dict.__getitem__(self, name)

    monkeypatch.setattr("app.db.seed.get_db", lambda: _FakeDb(users=_FakeCollection()))

    with pytest.raises(RuntimeError, match="seed_users"):
        seed_orders()


# --- order_type and the status lifecycle ---------------------------------------------------


def test_every_order_has_an_order_type():
    """How an order is fulfilled -- distinct from payment_method, which is how it's paid for."""
    from app.db.seed import ORDER_TYPES

    orders = generate_sample_orders(CUSTOMER_IDS, VENDOR_IDS, count_per_method=3)

    assert all(o["order_type"] in ORDER_TYPES for o in orders)


def test_order_type_and_payment_method_are_independent_fields():
    orders = generate_sample_orders(CUSTOMER_IDS, VENDOR_IDS, count_per_method=6)

    assert {o["order_type"] for o in orders} & {"delivery", "pickup", "dine_in"}
    assert {o["payment_method"] for o in orders} == set(PAYMENT_METHOD_MAP)


def test_terminal_and_incomplete_statuses_partition_the_status_list():
    """One definition of "incomplete" drives both the seed data and the schema annotation. If
    these drifted, a status could be in neither set and silently belong to no question."""
    from app.db.seed import INCOMPLETE_STATUSES, STATUSES, TERMINAL_STATUSES

    assert not set(TERMINAL_STATUSES) & set(INCOMPLETE_STATUSES)
    assert set(STATUSES) <= set(TERMINAL_STATUSES) | set(INCOMPLETE_STATUSES)


def test_the_data_contains_enough_incomplete_orders_to_report_on():
    """An "incomplete orders" question against data that is 95% completed returns almost
    nothing and looks broken."""
    from app.db.seed import INCOMPLETE_STATUSES

    orders = generate_sample_orders(CUSTOMER_IDS, VENDOR_IDS, count_per_method=12)
    incomplete = [o for o in orders if o["status"] in INCOMPLETE_STATUSES]

    assert len(incomplete) >= 10, "not enough in-flight orders for a 'last 10 incomplete' report"


def test_both_finished_and_in_flight_orders_are_present():
    from app.db.seed import INCOMPLETE_STATUSES, TERMINAL_STATUSES

    statuses = {o["status"] for o in generate_sample_orders(CUSTOMER_IDS, VENDOR_IDS, 12)}

    assert statuses & set(TERMINAL_STATUSES)
    assert statuses & set(INCOMPLETE_STATUSES)


def test_build_payby_rejects_an_unknown_payment_method():
    """PAYMENT_METHOD_MAP and build_payby must stay in step -- a method added to one and not the
    other would otherwise produce a payby that doesn't sum to the amount."""
    with pytest.raises(ValueError, match="Unknown payment_method"):
        build_payby("crypto", 100.0, random.Random(0))


def test_seed_orders_inserts_and_ensures_indexes(monkeypatch):
    class _UsersCollection:
        def distinct(self, field, filter_):
            return ["USR-1"] if filter_ == {"usertype": 1} else ["USR-2"]

    class _OrdersCollection:
        def __init__(self):
            self.deleted = False
            self.inserted = None

        def delete_many(self, filter_):
            self.deleted = True

        def insert_many(self, docs):
            self.inserted = docs
            return type("_Result", (), {"inserted_ids": list(range(len(docs)))})()

    orders = _OrdersCollection()

    class _FakeDb(dict):
        def __getitem__(self, name):
            return dict.__getitem__(self, name)

    index_calls = []
    monkeypatch.setattr(
        "app.db.seed.get_db", lambda: _FakeDb(users=_UsersCollection(), orders=orders)
    )
    monkeypatch.setattr("app.db.seed.ensure_indexes", lambda db: index_calls.append(db))

    inserted = seed_orders(count_per_method=2)

    assert inserted == 10  # 5 payment methods x 2
    assert orders.deleted is True
    assert index_calls, "indexes must be ensured after seeding"
    assert all(o["customer_id"] == "USR-1" for o in orders.inserted)


def test_seed_orders_keep_existing_skips_the_delete(monkeypatch):
    class _UsersCollection:
        def distinct(self, field, filter_):
            return ["USR-1"] if filter_ == {"usertype": 1} else ["USR-2"]

    class _OrdersCollection:
        def __init__(self):
            self.deleted = False

        def delete_many(self, filter_):
            self.deleted = True

        def insert_many(self, docs):
            return type("_Result", (), {"inserted_ids": list(range(len(docs)))})()

    orders = _OrdersCollection()

    class _FakeDb(dict):
        def __getitem__(self, name):
            return dict.__getitem__(self, name)

    monkeypatch.setattr(
        "app.db.seed.get_db", lambda: _FakeDb(users=_UsersCollection(), orders=orders)
    )
    monkeypatch.setattr("app.db.seed.ensure_indexes", lambda db: None)

    seed_orders(count_per_method=1, clear_existing=False)

    assert orders.deleted is False
