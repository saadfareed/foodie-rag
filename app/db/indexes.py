"""Idempotent index creation for the fields the agent graph filters on constantly.

Without these, every "orders for customer X", "orders from vendor Y", "pending orders", or
"orders since <date>" question (app/agents/query_agents.py generates filters on exactly these
fields) falls back to a full collection scan -- invisible at demo-data volumes, a real and
growing latency bottleneck once `orders`/`users` hold real production-sized data.
`Collection.create_index` is idempotent (a repeat call with the same key spec is a cheap no-op),
so this is safe to call on every process startup rather than treating it as a one-off migration
step.
"""

from pymongo.database import Database


def ensure_indexes(db: Database) -> None:
    db["orders"].create_index("customer_id")
    db["orders"].create_index("vendor_id")
    db["orders"].create_index("status")
    db["orders"].create_index("created_at")

    # user_id is how orders reference customers/vendors (see app/db/seed.py); usertype is the
    # discriminator every domain-scoped query forces into its filter (app/agents/domains.py ::
    # scope_spec_to_domain) -- both are on the hot path for every question.
    db["users"].create_index("user_id")
    db["users"].create_index("usertype")
    # Required for $near/$geoNear (app/db/executor.py); kept here too (not just in
    # app/db/seed_users.py) so it exists even if seeding was skipped against a pre-populated DB.
    db["users"].create_index([("location", "2dsphere")])
