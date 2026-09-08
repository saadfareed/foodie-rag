"""Central, code-owned registry mapping a semantic *domain* (what the classifier names) to the
physical Mongo collection and any forced scoping.

`customers` and `vendors` are two domains that share one physical `users` collection,
distinguished only by `usertype`. The classifier (app/agents/classifier.py) only ever emits a
domain name from DOMAIN_NAMES below -- never a raw collection name or a usertype value -- so a
hallucinated domain is rejected structurally, and a hallucinated cross-domain read is impossible
because scope_spec_to_domain() overwrites both the collection and the usertype filter
unconditionally, regardless of what the LLM's own QuerySpec says.
"""

from pydantic import BaseModel

from app.rag.query_spec import QuerySpec

SHARED_USER_FIELDS = [
    "user_id",
    "name",
    "usertype",
    "status",
    "city",
    "location",
    "created_at",
    "last_active_at",
]


class DomainConfig(BaseModel):
    name: str
    collection: str
    usertype: int | None = None
    geo_capable: bool = False
    geo_field: str | None = None
    # Fields to show a domain-scoped agent for a shared collection; None means "show the whole
    # collection's schema" (used by `orders`, which isn't shared with anything).
    schema_fields: list[str] | None = None
    # Preferred left-to-right column order for a generated CSV/XLSX/PDF table. Columns listed
    # here come first, in this order, when present on the row; anything else follows in
    # first-seen order. Columns absent from the rows are simply skipped.
    #
    # This exists because Mongo document key order is an implementation detail, not a reading
    # order. A reader scanning an order report wants to see who it's for and which order it is
    # before the payment internals, and that ordering shouldn't change because a projection
    # happened to emit keys differently.
    report_columns: list[str] | None = None
    # Columns that are correct data but noise in a human-facing report -- storage-level
    # encodings of something already shown in a friendlier form. Hidden from generated
    # CSV/XLSX/PDF tables only; the model still sees them, so it can still answer questions
    # about them.
    #
    # This is a *display* list, deliberately separate from app/security/field_policy.py: those
    # fields are withheld because showing them would be unsafe, these because showing them would
    # be unhelpful. Conflating the two would mean either leaking secrets or losing the ability
    # to answer questions about payment internals.
    report_hidden_columns: list[str] | None = None


DOMAINS: dict[str, DomainConfig] = {
    "orders": DomainConfig(
        name="orders",
        collection="orders",
        # customer_name/vendor_name are not stored on an order -- they're resolved from
        # customer_id/vendor_id by app/agents/enrichment.py before the report is built.
        report_columns=[
            "customer_name",
            "order_id",
            "amount",
            "order_type",
            "status",
            "vendor_name",
            "payment_method",
            "created_at",
        ],
        # onlinepaymentmethod (1/2) and isWallet are the storage encoding that payment_method
        # already names in words; payby is its per-method amount breakdown. All three are
        # answerable but none belong in a column a person reads.
        report_hidden_columns=["onlinepaymentmethod", "isWallet", "payby"],
    ),
    "customers": DomainConfig(
        name="customers",
        collection="users",
        usertype=1,
        geo_capable=True,
        geo_field="location",
        schema_fields=[*SHARED_USER_FIELDS, "loyalty_tier"],
        report_columns=["name", "user_id", "status", "city", "loyalty_tier", "last_active_at"],
    ),
    "vendors": DomainConfig(
        name="vendors",
        collection="users",
        usertype=2,
        geo_capable=True,
        geo_field="location",
        schema_fields=[*SHARED_USER_FIELDS, "business_name", "category", "rating"],
        report_columns=[
            "business_name",
            "user_id",
            "category",
            "rating",
            "status",
            "city",
        ],
    ),
}

DOMAIN_NAMES: tuple[str, ...] = tuple(DOMAINS.keys())


def geo_allowed_fields() -> dict[str, set[str]]:
    """collection -> set of field names queryable via geo_near, for app/rag/validator.py."""
    fields: dict[str, set[str]] = {}
    for domain in DOMAINS.values():
        if domain.geo_capable and domain.geo_field:
            fields.setdefault(domain.collection, set()).add(domain.geo_field)
    return fields


def allowed_collections() -> list[str]:
    return sorted({domain.collection for domain in DOMAINS.values()})


def report_columns_for(domain_name: str) -> list[str] | None:
    """Preferred report column order for a domain, or None if it has no preference."""
    domain = DOMAINS.get(domain_name)
    return domain.report_columns if domain else None


def report_hidden_columns_for(domain_name: str) -> set[str]:
    """Columns to omit from a generated report table for a domain (may be empty)."""
    domain = DOMAINS.get(domain_name)
    return set(domain.report_hidden_columns or []) if domain else set()


def merge_forced_filter(spec: QuerySpec, forced: dict) -> QuerySpec:
    """Merges a code-supplied (never LLM-supplied) filter predicate into a spec, applied to
    whichever shape the spec's operation actually uses: a $match stage prepended ahead of
    whatever the LLM generated (aggregate), or an $and wrapper around the LLM's own filter
    (find/count) -- either way, the forced predicate can't be overridden by anything the model
    put in its own filter/pipeline."""
    if not forced:
        return spec
    if spec.operation == "aggregate":
        spec.pipeline = [{"$match": forced}, *spec.pipeline]
    else:
        spec.filter = forced if not spec.filter else {"$and": [forced, spec.filter]}
    return spec


def scope_spec_to_domain(spec: QuerySpec, domain: DomainConfig) -> QuerySpec:
    """The single most important guardrail in this system: forces the spec's collection and
    usertype filter deterministically, in code, regardless of what the LLM produced. A
    "vendors" question literally cannot read customer rows even if the generated filter forgot
    -- or hallucinated the wrong -- discriminator value."""
    spec.collection = domain.collection
    if domain.usertype is not None:
        merge_forced_filter(spec, {"usertype": domain.usertype})
    return spec
