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


DOMAINS: dict[str, DomainConfig] = {
    "orders": DomainConfig(
        name="orders",
        collection="orders",
    ),
    "customers": DomainConfig(
        name="customers",
        collection="users",
        usertype=1,
        geo_capable=True,
        geo_field="location",
        schema_fields=[*SHARED_USER_FIELDS, "loyalty_tier"],
    ),
    "vendors": DomainConfig(
        name="vendors",
        collection="users",
        usertype=2,
        geo_capable=True,
        geo_field="location",
        schema_fields=[*SHARED_USER_FIELDS, "business_name", "category", "rating"],
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
