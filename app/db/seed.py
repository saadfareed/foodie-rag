"""Insert sample orders covering every payment-method combination.

Payment mapping (confirmed against real data):
    cash                -> onlinepaymentmethod=1, isWallet=False
    card                -> onlinepaymentmethod=2, isWallet=False
    wallet only         -> onlinepaymentmethod=1, isWallet=True
    hybrid wallet+cash  -> onlinepaymentmethod=1, isWallet=True
    hybrid card+wallet  -> onlinepaymentmethod=2, isWallet=True

`payby` breaks the order amount down by the component method(s) that paid it,
e.g. {"cash": 100} or {"wallet": 40, "card": 60}; its values always sum to `amount`.

Usage:
    python -m app.db.seed [--count-per-method 6]
"""

import argparse
import random
from datetime import datetime, timedelta, timezone

from app.db.mongo import get_db

PAYMENT_METHOD_MAP: dict[str, tuple[int, bool]] = {
    "cash": (1, False),
    "card": (2, False),
    "wallet": (1, True),
    "hybrid_wallet_cash": (1, True),
    "hybrid_card_wallet": (2, True),
}

STATUSES = ["completed", "completed", "completed", "pending", "refunded"]


def build_payment_fields(payment_method: str) -> tuple[int, bool]:
    return PAYMENT_METHOD_MAP[payment_method]


def build_payby(payment_method: str, amount: float, rng: random.Random) -> dict[str, float]:
    if payment_method == "cash":
        return {"cash": amount}
    if payment_method == "card":
        return {"card": amount}
    if payment_method == "wallet":
        return {"wallet": amount}

    if payment_method == "hybrid_wallet_cash":
        other_key = "cash"
    elif payment_method == "hybrid_card_wallet":
        other_key = "card"
    else:
        raise ValueError(f"Unknown payment_method: {payment_method}")

    wallet_part = round(amount * rng.uniform(0.2, 0.8), 2)
    other_part = round(amount - wallet_part, 2)
    return {"wallet": wallet_part, other_key: other_part}


def generate_sample_orders(count_per_method: int = 6, seed: int | None = 42) -> list[dict]:
    rng = random.Random(seed)  # nosec B311 - non-cryptographic demo data, seeded for reproducibility
    now = datetime.now(timezone.utc)
    orders: list[dict] = []
    order_seq = 1

    for payment_method, (onlinepaymentmethod, is_wallet) in PAYMENT_METHOD_MAP.items():
        for _ in range(count_per_method):
            amount = round(rng.uniform(5, 500), 2)
            created_at = now - timedelta(days=rng.randint(0, 30), hours=rng.randint(0, 23))
            orders.append(
                {
                    "order_id": f"ORD-{order_seq:05d}",
                    "amount": amount,
                    "payment_method": payment_method,
                    "onlinepaymentmethod": onlinepaymentmethod,
                    "isWallet": is_wallet,
                    "payby": build_payby(payment_method, amount, rng),
                    "status": rng.choice(STATUSES),
                    "created_at": created_at,
                }
            )
            order_seq += 1

    return orders


def seed_orders(count_per_method: int = 6, clear_existing: bool = True) -> int:
    db = get_db()
    if clear_existing:
        db["orders"].delete_many({})
    orders = generate_sample_orders(count_per_method)
    result = db["orders"].insert_many(orders)
    return len(result.inserted_ids)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count-per-method", type=int, default=6)
    parser.add_argument("--keep-existing", action="store_true")
    args = parser.parse_args()

    inserted = seed_orders(args.count_per_method, clear_existing=not args.keep_existing)
    print(f"Inserted {inserted} sample orders into 'orders'.")
