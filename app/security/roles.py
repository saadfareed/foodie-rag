"""Who is asking, and which rows they are allowed to be answered from.

This is the authorization half of the pair whose other half is `app/security/field_policy.py`.
That module decides which *fields* may leave the system for anyone; this one decides which *rows*
may leave it for a particular person. Both are code, in one place, applied deterministically --
never a prompt instruction, never a filter the model is asked to remember to include.

Three roles, mapping onto the `usertype` discriminator the data already carries (1 = customer,
2 = vendor -- see `app/agents/domains.py`), plus an operator (3) who is deliberately *not* one of
the two data domains:

    admin     every domain, unfiltered
    vendor    their own orders, their own vendor record, and the customers who ordered from them
    customer  their own orders, their own customer record, and the vendor directory

**The default is deny, not allow.** Before roles existed there was a single axis --
`authenticated_vendor_id`, which when absent forced no filter at all -- so "not signed in" meant
"sees everything". That was survivable while the only entry point was an allow-listed Slack
channel and it is not survivable now: a web session that failed to carry a scope would have been
an admin session. `ANONYMOUS` here can read nothing, and every caller must produce a Principal.

`DOMAIN_FILTERS` below is the whole policy. A new domain that isn't listed for a role is denied to
that role by omission, which is the safe direction to be wrong in.
"""

from dataclasses import dataclass
from enum import Enum


class Role(str, Enum):
    ADMIN = "admin"
    VENDOR = "vendor"
    CUSTOMER = "customer"
    #: Signed in to nothing. Kept as a real role rather than `None` so that "no access" is a
    #: value the type system carries, not the absence of one -- an unset scope is exactly how the
    #: previous design accidentally granted everything.
    ANONYMOUS = "anonymous"


#: usertype -> role, for identity resolved out of the `users` collection.
#:
#: 3 is an operator account. It exists so an admin can *sign in* like anyone else (an email, a
#: password, a status that can be suspended) without being answerable *about*: `app/agents/
#: domains.py` scopes the `customers` domain to usertype 1 and `vendors` to usertype 2, in code,
#: so a usertype-3 row is invisible to every question by construction rather than by a filter
#: anyone has to remember. An admin asserted by a host application still needs no row at all --
#: see `app/api/server.py::_check_session_request`, which skips the live-account check for admins
#: precisely because both shapes are legitimate.
USERTYPE_ROLES: dict[int, Role] = {1: Role.CUSTOMER, 2: Role.VENDOR, 3: Role.ADMIN}


def usertype_for(role: Role) -> int | None:
    """The `usertype` an account of `role` carries, or None if the role has no account form.

    The inverse of `USERTYPE_ROLES`, derived from it rather than written out again -- two
    hand-maintained tables that must agree is one table and a latent bug. `ANONYMOUS` has no
    usertype: it is the absence of an account, not a kind of one.
    """
    return next((usertype for usertype, r in USERTYPE_ROLES.items() if r is role), None)


@dataclass(frozen=True)
class Principal:
    """One authenticated identity, and the only input to every authorization decision."""

    role: Role = Role.ANONYMOUS
    #: The `users.user_id` this identity is. None for ANONYMOUS, and normally None for ADMIN --
    #: an admin's rows are never filtered, so there is nothing to scope it to. An operator who
    #: signed in with an account (usertype 3) carries their id here for the audit log; no
    #: authorization decision below reads it.
    user_id: str | None = None
    tenant_id: str = "default"
    display_name: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role is Role.ADMIN

    @property
    def cache_scope(self) -> str:
        """Namespaces the answer cache (`app/rag/answer_cache.py`).

        Role *and* id, not just id: two principals with the same `user_id` under different roles
        are answered from different rows, and a key carrying only the id would replay one's answer
        to the other. The previous key used the bare vendor id, which was sufficient only because
        there was exactly one role.
        """
        return f"{self.role.value}:{self.user_id or ''}"

    @property
    def audit_fields(self) -> dict:
        """What the audit log records about the identity a question was answered under.

        When authorization decides which rows an answer contains, "who asked" is not enough -- the
        role that authorised it is the thing an auditor needs, and it is not recoverable later.
        """
        return {"role": self.role.value, "principal_id": self.user_id}


ANONYMOUS = Principal(role=Role.ANONYMOUS)


def admin(tenant_id: str = "default", display_name: str | None = None) -> Principal:
    return Principal(role=Role.ADMIN, tenant_id=tenant_id, display_name=display_name)


#: Which domains each role may query at all. A domain missing from a role's set is refused before
#: any query is generated -- no Gemini call, no Mongo round-trip.
DOMAIN_ACCESS: dict[Role, frozenset[str]] = {
    Role.ADMIN: frozenset({"orders", "customers", "vendors"}),
    Role.VENDOR: frozenset({"orders", "customers", "vendors"}),
    # A customer may browse the vendor directory. That is deliberate and it is the one place this
    # policy is permissive: vendor name, city, category and rating are how a marketplace works,
    # and restricting vendors to "ones you have already ordered from" would break the first
    # question a new customer asks. Their *orders* and every other customer stay private.
    Role.CUSTOMER: frozenset({"orders", "customers", "vendors"}),
    Role.ANONYMOUS: frozenset(),
}


def may_query(principal: Principal, domain: str) -> bool:
    return domain in DOMAIN_ACCESS.get(principal.role, frozenset())


def forced_filter(
    principal: Principal,
    domain: str,
    *,
    authorized_customer_ids: list[str] | None = None,
) -> dict | None:
    """The filter that must be merged into a generated query for this principal and domain.

    Returns None when no row restriction applies (an admin, or a customer browsing vendors).
    Raises nothing: a domain the principal may not query at all is caught by `may_query` before
    a spec is ever generated.

    `authorized_customer_ids` is the vendor -> customers case, resolved separately because it is
    the one rule that needs a query to answer (see `app/agents/graph.py`). Passing None for a
    vendor asking about customers yields an impossible filter rather than an unfiltered one --
    failing closed is the only acceptable direction here.
    """
    if principal.role is Role.ADMIN:
        return None

    if principal.role is Role.VENDOR:
        if domain == "orders":
            return {"vendor_id": principal.user_id}
        if domain == "vendors":
            return {"user_id": principal.user_id}
        if domain == "customers":
            # Fail closed: an unresolved id list must not become "no filter".
            return {"user_id": {"$in": authorized_customer_ids or []}}

    if principal.role is Role.CUSTOMER:
        if domain == "orders":
            return {"customer_id": principal.user_id}
        if domain == "customers":
            return {"user_id": principal.user_id}
        if domain == "vendors":
            return None  # the directory -- see DOMAIN_ACCESS above

    # Unreached for known roles; a new role or domain lands here and is refused by the caller.
    return {"user_id": "__no_such_user__"}


def needs_authorized_customers(principal: Principal, domain: str) -> bool:
    """True when this (principal, domain) pair can only be filtered after a lookup.

    Exactly one rule needs one today: a vendor asking about customers may see the customers who
    ordered from them, which is a set that has to be read out of their own orders first.
    """
    return principal.role is Role.VENDOR and domain == "customers"


#: Domains on which a role's rows are restricted to *its own* people, and where contact details
#: may therefore appear in a generated report.
#:
#: This is not the same question as `DOMAIN_ACCESS`. A customer may read the vendor directory --
#: that is deliberate -- but the directory is unfiltered, so contact details there would be a
#: scrapable list of every vendor's phone number. The rule is: contacts are visible only where
#: the principal's own filter already narrowed the rows to people they have a relationship with.
_CONTACT_VISIBLE_DOMAINS: dict[Role, frozenset[str]] = {
    # An admin already sees every row; withholding the column would protect nothing.
    Role.ADMIN: frozenset({"orders", "customers", "vendors"}),
    # Their own record, and the customers who ordered from them -- both already narrowed.
    Role.VENDOR: frozenset({"customers", "vendors"}),
    # Their own record only. Not `vendors`: that is the unfiltered directory.
    Role.CUSTOMER: frozenset({"customers"}),
    Role.ANONYMOUS: frozenset(),
}


def may_see_contacts(principal: Principal, domain: str) -> bool:
    """Whether a generated report for this principal may carry contact columns for `domain`.

    Reports only. The model is never shown these fields -- `app/security/field_policy.py` drops
    them from every row on the way out of MongoDB -- so this cannot make the bot answer a question
    about someone's phone number. It governs a column added in code, after the answer, to rows the
    principal was already authorized to see.

    That split is the point. Bulk extraction through a natural-language prompt is the threat;
    a vendor exporting their own customer list is the legitimate use, and it carries an audit
    trail and a bounded row set that a free-text answer never could.
    """
    return domain in _CONTACT_VISIBLE_DOMAINS.get(principal.role, frozenset())
