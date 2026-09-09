"""Idempotent index creation for the fields the agent graph filters on constantly.

Without these, every "orders for customer X", "orders from vendor Y", "pending orders", or
"orders since <date>" question (app/agents/query_agents.py generates filters on exactly these
fields) falls back to a full collection scan -- invisible at demo-data volumes, a real and
growing latency bottleneck once `orders`/`users` hold real production-sized data.
`Collection.create_index` is idempotent (a repeat call with the same key spec is a cheap no-op),
so this is safe to call on every process startup rather than treating it as a one-off migration
step.

**Compound indexes carry most of the value here.** Real questions almost never filter on one
field: "pending orders this week" is `status` *and* `created_at`, and "vendor X's orders last
month" is `vendor_id` *and* `created_at`. Two single-field indexes can only be intersected,
which is far slower than one index that already holds the pair in sorted order.

Field order within each compound index follows the equality-then-range rule: the field matched
for equality (`status`, `vendor_id`) comes first, the field matched as a range (`created_at`)
second. A compound index also serves any *prefix* of itself, so `(vendor_id, created_at)` covers
a plain `vendor_id` lookup too -- which is why the standalone single-field indexes those pairs
subsume are not created separately.
"""

from pymongo.database import Database


def ensure_indexes(db: Database) -> None:
    orders = db["orders"]
    # Prefixes cover the single-field `customer_id` / `vendor_id` / `status` lookups too, so
    # those are deliberately not created on their own.
    orders.create_index([("customer_id", 1), ("created_at", -1)])
    orders.create_index([("vendor_id", 1), ("created_at", -1)])
    orders.create_index([("status", 1), ("created_at", -1)])
    # Date-only questions ("orders last week") match no equality field, so they need created_at
    # leading. Descending, matching the "most recent first" sort these questions ask for.
    orders.create_index([("created_at", -1)])

    # user_id is how orders reference customers/vendors (see app/db/seed.py); usertype is the
    # discriminator every domain-scoped query forces into its filter (app/agents/domains.py ::
    # scope_spec_to_domain) -- both are on the hot path for every question.
    users = db["users"]
    users.create_index("user_id")
    # usertype leads every user query because scope_spec_to_domain forces it in unconditionally,
    # so pairing it with the fields questions actually filter on is what keeps those queries off
    # a collection scan.
    users.create_index([("usertype", 1), ("city", 1)])
    users.create_index([("usertype", 1), ("status", 1)])
    # Required for $near/$geoNear (app/db/executor.py); kept here too (not just in
    # app/db/seed_users.py) so it exists even if seeding was skipped against a pre-populated DB.
    # A 2dsphere key must lead its compound index, so the usertype filter cannot be folded in
    # ahead of it -- Mongo applies the geo index first and filters the (already small) result.
    users.create_index([("location", "2dsphere")])
    # Sign-in looks a user up by exact email (app/db/identity.py). Sparse because only accounts
    # that can sign in carry the field, and unique because two accounts sharing an address would
    # make "which identity did this person prove?" ambiguous -- the one question authentication
    # exists to answer. Partial rather than plain-sparse-unique so that many documents without an
    # email don't collide on null.
    users.create_index(
        "email",
        unique=True,
        partialFilterExpression={"email": {"$type": "string"}},
        name="email_unique_when_present",
    )
