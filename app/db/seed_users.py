"""Insert sample users covering both usertypes (1=customer, 2=vendor).

usertype discriminator:
    1 -> customer
    2 -> vendor

Both usertypes share the same collection and location schema (GeoJSON Point,
so $near/$geoWithin work identically for "nearby customers" and "nearby
vendors"). Vendor-only fields (business_name, category, rating) and
customer-only fields (loyalty_tier) are simply absent on the other usertype's
documents rather than null, matching how the demo `orders` data models
optional fields.

Usage:
    python -m app.db.seed_users [--customers 30 --vendors 20]
"""

import argparse
import random
from datetime import datetime, timedelta, timezone

from app.db.indexes import ensure_indexes
from app.db.mongo import get_db

# (city label, center lng, center lat) -- users are scattered within ~0.15 degrees
# (roughly 15km) of these centers so "nearby X" geo queries have realistic clusters.
CITIES = [
    ("Karachi", 67.0011, 24.8607),
    ("Lahore", 74.3587, 31.5204),
    ("Islamabad", 73.0479, 33.6844),
]

VENDOR_CATEGORIES = ["restaurant", "grocery", "pharmacy", "electronics", "clothing"]
LOYALTY_TIERS = ["bronze", "silver", "gold"]
STATUSES = ["active", "active", "active", "inactive", "suspended"]

# Real-looking person names, so a report reads "Ayesha Khan" rather than "Customer 00007".
# A placeholder name is fine for a geo query but useless in a customer-facing report -- and it
# hides formatting bugs (column widths, truncation, non-ASCII handling) that only show up once
# names vary in length. Names are drawn from the regions the CITIES above cover.
FIRST_NAMES = [
    "Ayesha",
    "Bilal",
    "Fatima",
    "Hamza",
    "Iqra",
    "Junaid",
    "Khadija",
    "Lubna",
    "Mahnoor",
    "Noman",
    "Omar",
    "Rabia",
    "Saad",
    "Sana",
    "Tariq",
    "Usman",
    "Wajiha",
    "Yasir",
    "Zainab",
    "Zohaib",
    "Adeel",
    "Hina",
    "Imran",
    "Nadia",
]
LAST_NAMES = [
    "Ahmed",
    "Akhtar",
    "Ali",
    "Aslam",
    "Baig",
    "Chaudhry",
    "Farooq",
    "Hussain",
    "Iqbal",
    "Javed",
    "Khan",
    "Malik",
    "Mirza",
    "Qureshi",
    "Raza",
    "Sheikh",
    "Siddiqui",
    "Tanveer",
    "Yousaf",
    "Zafar",
]

# Vendor business names are built from these rather than "<person name> Store", so a vendor
# column in a report reads like a real business and is visibly distinct from a customer name.
BUSINESS_PREFIXES = [
    "Al-Noor",
    "Bismillah",
    "City",
    "Crescent",
    "Diamond",
    "Eastern",
    "Gulberg",
    "Karachi",
    "Lahore",
    "Metro",
    "New",
    "Pak",
    "Royal",
    "Shalimar",
    "Star",
]
BUSINESS_SUFFIXES = {
    "restaurant": ["Restaurant", "Kitchen", "Grill", "Cafe", "Biryani House"],
    "grocery": ["Grocers", "Mart", "Superstore", "Provisions", "Cash & Carry"],
    "pharmacy": ["Pharmacy", "Medicos", "Chemists", "Drug Store", "Medical Store"],
    "electronics": ["Electronics", "Traders", "Tech Hub", "Appliances", "Gadgets"],
    "clothing": ["Fabrics", "Garments", "Boutique", "Textiles", "Collection"],
}


def _scatter(rng: random.Random, center_lng: float, center_lat: float) -> dict:
    lng = round(center_lng + rng.uniform(-0.15, 0.15), 6)
    lat = round(center_lat + rng.uniform(-0.15, 0.15), 6)
    return {"type": "Point", "coordinates": [lng, lat]}


def generate_sample_users(
    customers: int = 30, vendors: int = 20, seed: int | None = 42
) -> list[dict]:
    rng = random.Random(seed)  # nosec B311 - non-cryptographic demo data, seeded for reproducibility
    now = datetime.now(timezone.utc)
    users: list[dict] = []
    seq = 1

    def base_doc(usertype: int) -> dict:
        nonlocal seq
        city, center_lng, center_lat = rng.choice(CITIES)
        created_at = now - timedelta(days=rng.randint(1, 365))
        last_active_at = created_at + timedelta(days=rng.randint(0, 365))
        if last_active_at > now:
            # Reclamp into [created_at, now] rather than a fixed "now minus up to 5 days" --
            # that fixed lookback could land *before* created_at whenever created_at itself was
            # within the last few days, producing last_active_at < created_at.
            span_seconds = (now - created_at).total_seconds()
            last_active_at = created_at + timedelta(seconds=rng.uniform(0, span_seconds))
        doc = {
            "user_id": f"USR-{seq:05d}",
            "name": f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}",
            "usertype": usertype,
            "status": rng.choice(STATUSES),
            "city": city,
            "location": _scatter(rng, center_lng, center_lat),
            "created_at": created_at,
            "last_active_at": last_active_at,
        }
        seq += 1
        return doc

    for _ in range(customers):
        doc = base_doc(1)
        doc["loyalty_tier"] = rng.choice(LOYALTY_TIERS)
        users.append(doc)

    for _ in range(vendors):
        doc = base_doc(2)
        category = rng.choice(VENDOR_CATEGORIES)
        doc["category"] = category
        # `name` stays the owner's personal name; `business_name` is what a report shows in a
        # "vendor" column, so the two must not read the same way.
        doc["business_name"] = (
            f"{rng.choice(BUSINESS_PREFIXES)} {rng.choice(BUSINESS_SUFFIXES[category])}"
        )
        doc["rating"] = round(rng.uniform(3.0, 5.0), 1)
        users.append(doc)

    return users


def seed_users(customers: int = 30, vendors: int = 20, clear_existing: bool = True) -> int:
    db = get_db()
    if clear_existing:
        db["users"].delete_many({})
    users = generate_sample_users(customers, vendors)
    result = db["users"].insert_many(users)
    # Covers the 2dsphere geo index plus user_id/usertype -- see app/db/indexes.py.
    ensure_indexes(db)
    return len(result.inserted_ids)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--customers", type=int, default=30)
    parser.add_argument("--vendors", type=int, default=20)
    parser.add_argument("--keep-existing", action="store_true")
    args = parser.parse_args()

    inserted = seed_users(args.customers, args.vendors, clear_existing=not args.keep_existing)
    print(f"Inserted {inserted} sample users into 'users' (indexes ensured).")
