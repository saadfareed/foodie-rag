"""Insert sample orders covering every payment-method combination.

Payment mapping (confirmed against real data):
    cash                -> onlinepaymentmethod=1, isWallet=False
    card                -> onlinepaymentmethod=2, isWallet=False
    wallet only         -> onlinepaymentmethod=1, isWallet=True
    hybrid wallet+cash  -> onlinepaymentmethod=1, isWallet=True
    hybrid card+wallet  -> onlinepaymentmethod=2, isWallet=True

`payby` breaks the order amount down by the component method(s) that paid it,
e.g. {"cash": 100} or {"wallet": 40, "card": 60}; its values always sum to `amount`.

Each order also gets a customer_id/vendor_id referencing users.user_id (see
app/db/seed_users.py), so cross-domain questions like "vendors near a customer with pending
orders" have real fields to join on. Run seed_users FIRST -- this reads the live `users`
collection for its pool of customer/vendor ids and fails clearly if it's empty.

Usage:
    python -m app.db.seed_users
    python -m app.db.seed [--count-per-method 12]
"""

import argparse
import random
from datetime import datetime, timedelta, timezone

from app.db.indexes import ensure_indexes
from app.db.mongo import get_db

PAYMENT_METHOD_MAP: dict[str, tuple[int, bool]] = {
    "cash": (1, False),
    "card": (2, False),
    "wallet": (1, True),
    "hybrid_wallet_cash": (1, True),
    "hybrid_card_wallet": (2, True),
}

# Order lifecycle. TERMINAL_STATUSES are the ones where nothing further will happen; everything
# else is "in flight" and is what a question about *incomplete* / outstanding / open / pending
# orders is asking for. Keeping both sets here (rather than only a flat list) means the same
# definition drives the seed data and the schema annotation shown to the model, so "incomplete"
# can't come to mean two different things.
TERMINAL_STATUSES = ["completed", "delivered", "cancelled", "refunded"]
INCOMPLETE_STATUSES = ["pending", "confirmed", "preparing", "out_for_delivery", "on_hold"]

# Weighted so most orders are finished but there's always a healthy set of in-flight ones to
# report on -- an "incomplete orders" question against data that is 95% completed returns almost
# nothing and looks broken.
STATUSES = [
    "completed",
    "completed",
    "delivered",
    "delivered",
    "cancelled",
    "refunded",
    "pending",
    "pending",
    "confirmed",
    "preparing",
    "out_for_delivery",
    "on_hold",
]

# How the order is fulfilled -- distinct from payment_method (how it's paid for).
ORDER_TYPES = ["delivery", "delivery", "delivery", "pickup", "dine_in"]


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


def generate_sample_orders(
    customer_ids: list[str],
    vendor_ids: list[str],
    count_per_method: int = 12,
    seed: int | None = 42,
) -> list[dict]:
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
                    "customer_id": rng.choice(customer_ids),
                    "vendor_id": rng.choice(vendor_ids),
                    "amount": amount,
                    "order_type": rng.choice(ORDER_TYPES),
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


def seed_orders(count_per_method: int = 12, clear_existing: bool = True) -> int:
    db = get_db()
    customer_ids = db["users"].distinct("user_id", {"usertype": 1})
    vendor_ids = db["users"].distinct("user_id", {"usertype": 2})
    if not customer_ids or not vendor_ids:
        raise RuntimeError(
            "No customers/vendors found in 'users' -- run `python -m app.db.seed_users` first "
            "so orders have real customer_id/vendor_id values to reference."
        )

    if clear_existing:
        db["orders"].delete_many({})
    orders = generate_sample_orders(customer_ids, vendor_ids, count_per_method)
    result = db["orders"].insert_many(orders)
    ensure_indexes(db)
    return len(result.inserted_ids)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count-per-method", type=int, default=12)
    parser.add_argument("--keep-existing", action="store_true")
    args = parser.parse_args()

    inserted = seed_orders(args.count_per_method, clear_existing=not args.keep_existing)
    print(f"Inserted {inserted} sample orders into 'orders'.")
