import random

from app.db.seed import (
    PAYMENT_METHOD_MAP,
    build_payby,
    build_payment_fields,
    generate_sample_orders,
)


def test_payment_method_mapping_matches_spec():
    assert build_payment_fields("cash") == (1, False)
    assert build_payment_fields("card") == (2, False)
    assert build_payment_fields("wallet") == (1, True)
    assert build_payment_fields("hybrid_card_wallet") == (2, True)
    assert build_payment_fields("hybrid_wallet_cash") == (1, True)


def test_generate_sample_orders_count_per_method():
    orders = generate_sample_orders(count_per_method=3)
    assert len(orders) == 3 * len(PAYMENT_METHOD_MAP)
    for method in PAYMENT_METHOD_MAP:
        assert sum(1 for o in orders if o["payment_method"] == method) == 3


def test_generate_sample_orders_fields_match_mapping():
    orders = generate_sample_orders(count_per_method=2)
    for order in orders:
        expected_code, expected_wallet = PAYMENT_METHOD_MAP[order["payment_method"]]
        assert order["onlinepaymentmethod"] == expected_code
        assert order["isWallet"] == expected_wallet
        assert order["amount"] > 0
        assert order["order_id"].startswith("ORD-")


def test_generate_sample_orders_ids_are_unique():
    orders = generate_sample_orders(count_per_method=4)
    ids = [o["order_id"] for o in orders]
    assert len(ids) == len(set(ids))


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
    orders = generate_sample_orders(count_per_method=6)
    for order in orders:
        assert round(sum(order["payby"].values()), 2) == order["amount"]
