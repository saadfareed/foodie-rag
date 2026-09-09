"""The row-level access policy (app/security/roles.py).

This module is the authorization half of the pair whose other half is the field policy, and its
failure mode is the quiet one: a wrong filter here does not raise, does not log, and produces a
confident, well-formatted answer built from rows the asker was never entitled to see. So the
tests are written as a table of what each role may see, one case per cell, including the cells
that must produce *nothing*.

The single most important test in the file is
`test_a_vendor_with_no_resolved_customers_gets_an_impossible_filter` -- the one place where a
missing value could plausibly be read as "no restriction".
"""

import pytest

from app.security.roles import (
    ANONYMOUS,
    DOMAIN_ACCESS,
    USERTYPE_ROLES,
    Principal,
    Role,
    admin,
    forced_filter,
    may_query,
    may_see_contacts,
    needs_authorized_customers,
)

VENDOR = Principal(role=Role.VENDOR, user_id="USR-V1")
CUSTOMER = Principal(role=Role.CUSTOMER, user_id="USR-C1")
ADMIN = admin()


# --- who may open which door ------------------------------------------------------------------


@pytest.mark.parametrize("domain", ["orders", "customers", "vendors"])
def test_an_anonymous_principal_may_read_nothing(domain):
    """The default is deny. Before roles existed, an absent scope meant "no filter" -- so a web
    session that failed to carry one would have been an admin session."""
    assert may_query(ANONYMOUS, domain) is False


@pytest.mark.parametrize("domain", ["orders", "customers", "vendors"])
@pytest.mark.parametrize("principal", [ADMIN, VENDOR, CUSTOMER])
def test_every_signed_in_role_may_open_every_domain(principal, domain):
    """Access is not what separates the roles -- the filter is. Refusing a domain outright is
    reserved for the anonymous case; everyone else gets their own slice of it."""
    assert may_query(principal, domain) is True


def test_an_unknown_domain_is_denied_by_omission():
    """A new domain added to the registry is invisible to every role until it is listed here,
    which is the safe direction to be wrong in."""
    assert may_query(ADMIN, "invoices") is False


def test_the_role_table_covers_every_role():
    missing = [role for role in Role if role not in DOMAIN_ACCESS]

    assert not missing, f"roles with no entry in DOMAIN_ACCESS: {missing}"


def test_usertype_maps_to_the_roles_the_data_already_encodes():
    """1 and 2 are the discriminator app/agents/domains.py already scopes on -- identity resolved
    from `users` must agree with it, or a vendor would be answered as a customer. 3 is an
    operator: an account that can sign in, and that neither data domain can see."""
    assert USERTYPE_ROLES == {1: Role.CUSTOMER, 2: Role.VENDOR, 3: Role.ADMIN}


def test_an_operator_account_belongs_to_no_data_domain():
    """The whole safety of letting an admin have a `users` row: `customers` is scoped to usertype
    1 and `vendors` to usertype 2, in code, so a usertype-3 row cannot appear in an answer
    whatever anyone asks."""
    from app.agents.domains import DOMAINS

    scoped = {d.usertype for d in DOMAINS.values() if d.collection == "users"}

    assert scoped == {1, 2}


# --- admin ------------------------------------------------------------------------------------


@pytest.mark.parametrize("domain", ["orders", "customers", "vendors"])
def test_an_admin_is_never_filtered(domain):
    assert forced_filter(ADMIN, domain) is None


# --- vendor -----------------------------------------------------------------------------------


def test_a_vendor_sees_only_their_own_orders():
    assert forced_filter(VENDOR, "orders") == {"vendor_id": "USR-V1"}


def test_a_vendor_sees_only_their_own_vendor_record():
    assert forced_filter(VENDOR, "vendors") == {"user_id": "USR-V1"}


def test_a_vendor_sees_the_customers_who_ordered_from_them():
    assert forced_filter(VENDOR, "customers", authorized_customer_ids=["C1", "C2"]) == {
        "user_id": {"$in": ["C1", "C2"]}
    }


def test_a_vendor_with_no_resolved_customers_gets_an_impossible_filter():
    """The most important case in this file.

    A vendor's customer scope has to be computed, so there is a real code path where it hasn't
    been. If "unresolved" collapsed to "no filter", that path would hand a vendor every customer
    in the database -- and it would do it silently, with a correct-looking answer. `$in: []`
    matches nothing, so the failure is visibly empty rather than invisibly total.
    """
    assert forced_filter(VENDOR, "customers") == {"user_id": {"$in": []}}
    assert forced_filter(VENDOR, "customers", authorized_customer_ids=[]) == {
        "user_id": {"$in": []}
    }


def test_only_the_vendor_customers_pair_needs_a_lookup():
    assert needs_authorized_customers(VENDOR, "customers") is True
    assert needs_authorized_customers(VENDOR, "orders") is False
    assert needs_authorized_customers(CUSTOMER, "customers") is False
    assert needs_authorized_customers(ADMIN, "customers") is False


# --- customer ---------------------------------------------------------------------------------


def test_a_customer_sees_only_their_own_orders():
    assert forced_filter(CUSTOMER, "orders") == {"customer_id": "USR-C1"}


def test_a_customer_sees_only_their_own_customer_record():
    """Without this a customer could list every other customer -- the domain has no natural
    restriction of its own, and before roles there was no filter here at all."""
    assert forced_filter(CUSTOMER, "customers") == {"user_id": "USR-C1"}


def test_a_customer_browses_the_vendor_directory_unfiltered():
    """The one deliberate permission in the table. Vendor name, city, category and rating are how
    a marketplace works; restricting it to "vendors you have already ordered from" would break
    the first question a new customer asks."""
    assert forced_filter(CUSTOMER, "vendors") is None


# --- anonymous / unknown ------------------------------------------------------------------------


@pytest.mark.parametrize("domain", ["orders", "customers", "vendors"])
def test_an_anonymous_principal_never_gets_a_permissive_filter(domain):
    """may_query already refuses these, but a filter is the last line: if a new caller ever
    reaches forced_filter without checking access first, it must not be handed None."""
    assert forced_filter(ANONYMOUS, domain) != None  # noqa: E711 -- None is the failure here


# --- cache scoping ------------------------------------------------------------------------------


def test_the_cache_scope_separates_roles_sharing_an_id():
    """The answer cache is keyed by this. Two principals with the same user_id under different
    roles are answered from different rows, so a key carrying only the id would replay one's
    answer to the other."""
    same_id_vendor = Principal(role=Role.VENDOR, user_id="USR-1")
    same_id_customer = Principal(role=Role.CUSTOMER, user_id="USR-1")

    assert same_id_vendor.cache_scope != same_id_customer.cache_scope


def test_the_audit_fields_name_the_role_that_authorised_the_answer():
    assert VENDOR.audit_fields == {"role": "vendor", "principal_id": "USR-V1"}


# --- contact details in reports ---------------------------------------------------------------


def test_a_vendor_may_see_contacts_for_their_own_customers():
    """Their customers domain is already narrowed to people who ordered from them, so a contact
    column there is a list they have a relationship with, not a scrape."""
    assert may_see_contacts(VENDOR, "customers") is True
    assert may_see_contacts(VENDOR, "vendors") is True


def test_a_customer_may_not_see_contacts_in_the_vendor_directory():
    """The exception that matters. A customer reads `vendors` *unfiltered* -- that is deliberate,
    it is a directory -- so a contact column there would be every vendor's phone number in one
    download."""
    assert may_see_contacts(CUSTOMER, "vendors") is False
    assert may_see_contacts(CUSTOMER, "customers") is True


def test_an_anonymous_principal_sees_no_contacts_anywhere():
    for domain in ("orders", "customers", "vendors"):
        assert may_see_contacts(ANONYMOUS, domain) is False


def test_contact_visibility_is_narrower_than_read_access():
    """These are different questions, and conflating them is how the directory leaks. Read access
    says a role may see the rows; contact visibility says its own filter already narrowed them."""
    readable = {d for d in ("orders", "customers", "vendors") if may_query(CUSTOMER, d)}
    contactable = {d for d in ("orders", "customers", "vendors") if may_see_contacts(CUSTOMER, d)}

    assert contactable < readable
