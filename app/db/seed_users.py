"""Insert sample users covering every usertype (1=customer, 2=vendor, 3=operator/admin).

usertype discriminator:
    1 -> customer
    2 -> vendor
    3 -> operator (admin). One account, so somebody can sign in to the playground as an admin.
         It is deliberately not a data domain: `app/agents/domains.py` scopes `customers` to
         usertype 1 and `vendors` to usertype 2, so this row is invisible to every question.

Both usertypes share the same collection and location schema (GeoJSON Point,
so $near/$geoWithin work identically for "nearby customers" and "nearby
vendors"). Vendor-only fields (business_name, category, rating) and
customer-only fields (loyalty_tier) are simply absent on the other usertype's
documents rather than null, matching how the demo `orders` data models
optional fields.

Each user also gets an `email`, a `phone` and a `password_hash`, which exist only so a person can
prove who they are (app/db/identity.py). The first two are dropped from every query result by the
field policy and the third is redacted, so the bot cannot be asked for any of them -- see
app/security/field_policy.py.

Every seeded account shares one password (`--password`, default `test123`) because these are
throwaway demo rows on a reserved TLD. Each one is hashed separately, with its own salt, so the
stored shape is the real thing rather than a shortcut that would teach the wrong lesson.

Usage:
    python -m app.db.seed_users [--customers 30 --vendors 20] [--password test123]
"""

import argparse
import random
from datetime import datetime, timedelta, timezone

from app.db.indexes import ensure_indexes
from app.db.mongo import get_db
from app.security.passwords import hash_password

# (city label, center lng, center lat) -- users are scattered within ~0.15 degrees
# (roughly 15km) of these centers so "nearby X" geo queries have realistic clusters.
CITIES = [
    ("Karachi", 67.0011, 24.8607),
    ("Lahore", 74.3587, 31.5204),
    ("Islamabad", 73.0479, 33.6844),
]

#: The password every seeded account signs in with. Demo data on a reserved TLD, printed by the
#: seeder so it is discoverable rather than guessed -- and useless anywhere real, because these
#: rows are.
DEFAULT_SEED_PASSWORD = "test123"  # nosec B105 - demo credential for throwaway seed rows
#: The one operator account, at a memorable address: an admin id nobody can guess is an admin
#: nobody can sign in as.
ADMIN_EMAIL = "admin@example.test"

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
    customers: int = 30,
    vendors: int = 20,
    seed: int | None = 42,
    password: str = DEFAULT_SEED_PASSWORD,
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
        user_id = f"USR-{seq:05d}"
        doc = {
            "user_id": user_id,
            "name": f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}",
            # Sign-in address. Derived from the id rather than the name so it is predictable
            # enough to actually log in with while demoing (USR-00031 -> usr-00031@example.test),
            # and on a reserved TLD so nothing here can ever reach a real inbox.
            #
            # The field policy drops `email`/`phone` from every row before anything downstream
            # sees them (app/security/field_policy.py), so seeding them does not make them
            # answerable -- app/db/identity.py is the one code path allowed to read them.
            "email": f"{user_id.lower()}@example.test",
            "phone": f"+92300{seq:07d}",
            # Hashed per user rather than once and copied, so each row carries its own salt --
            # the shape a real deployment has. app/security/passwords.py owns the format.
            "password_hash": hash_password(password),
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

    # One operator. `status` is forced active rather than drawn from STATUSES: a suspended admin
    # cannot sign in, and a demo whose admin account randomly doesn't work is a bug report.
    admin = base_doc(3)
    admin["name"] = "Operator"
    admin["email"] = ADMIN_EMAIL
    admin["status"] = "active"
    users.append(admin)

    return users


def seed_users(
    customers: int = 30,
    vendors: int = 20,
    clear_existing: bool = True,
    password: str = DEFAULT_SEED_PASSWORD,
) -> int:
    db = get_db()
    if clear_existing:
        db["users"].delete_many({})
    users = generate_sample_users(customers, vendors, password=password)
    result = db["users"].insert_many(users)
    # Covers the 2dsphere geo index plus user_id/usertype -- see app/db/indexes.py.
    ensure_indexes(db)
    return len(result.inserted_ids)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--customers", type=int, default=30)
    parser.add_argument("--vendors", type=int, default=20)
    parser.add_argument("--keep-existing", action="store_true")
    parser.add_argument("--password", default=DEFAULT_SEED_PASSWORD)
    args = parser.parse_args()

    inserted = seed_users(
        args.customers,
        args.vendors,
        clear_existing=not args.keep_existing,
        password=args.password,
    )
    print(f"Inserted {inserted} sample users into 'users' (indexes ensured).")
    # Printed, not documented elsewhere: the addresses are derived from generated ids, so without
    # this the only way to find one to sign in with is to query the database by hand.
    print(f"Every account signs in with the password: {args.password}")
    print(f"  customer  usr-00001@example.test  (through usr-{args.customers:05d}@example.test)")
    print(f"  vendor    usr-{args.customers + 1:05d}@example.test")
    print(f"  admin     {ADMIN_EMAIL}")
