"""Resolving a sign-in to an identity (app/db/identity.py).

This is the only code path allowed to read `users.email` and `users.password_hash` -- fields
`app/security/field_policy.py` drops or redacts before anything downstream sees them. So these
tests are as much about what the functions *refuse* to do as what they return: in particular, that
every way a sign-in can fail is one indistinguishable answer, because any difference between them
turns a login form into a way to test whether an address is registered.
"""

import pytest

from app.db.identity import (
    AccountNotActiveError,
    authenticate_password,
    fetch_contacts,
    find_principal_by_email,
    normalize_email,
    principal_exists,
)
from app.security.passwords import hash_password
from app.security.roles import Role

#: Cheap on purpose -- these tests exercise the branches, not the cost factor.
PASSWORD = "correct horse battery staple"
HASH = hash_password(PASSWORD, iterations=1)


class _FakeUsers:
    """Records the filter and projection it was called with. Minimal on purpose -- `find_one` is
    the only method the subject uses."""

    def __init__(self, row=None):
        self._row = row
        self.calls = []

    def find_one(self, filter_, projection=None):
        self.calls.append({"filter": filter_, "projection": projection})
        return self._row


class _FakeDb:
    def __init__(self, users):
        self._users = users

    def __getitem__(self, name):
        assert name == "users"
        return self._users


def _db(row=None):
    users = _FakeUsers(row)
    return _FakeDb(users), users


# --- the happy paths ---------------------------------------------------------------------------


def test_a_customer_address_resolves_to_a_customer_principal():
    db, _ = _db({"user_id": "USR-9", "usertype": 1, "name": "Ayesha Khan", "status": "active"})

    principal = find_principal_by_email(db, "usr-9@example.test")

    assert (principal.role, principal.user_id, principal.display_name) == (
        Role.CUSTOMER,
        "USR-9",
        "Ayesha Khan",
    )


def test_a_vendor_address_resolves_to_a_vendor_principal():
    db, _ = _db({"user_id": "USR-31", "usertype": 2, "name": "Kifayat Foods", "status": "active"})

    assert find_principal_by_email(db, "usr-31@example.test").role is Role.VENDOR


@pytest.mark.parametrize("address", ["  USR-9@Example.Test  ", "usr-9@example.test"])
def test_addresses_are_normalised_before_lookup(address):
    """Otherwise "Ayesha@..." and "ayesha@..." are two accounts, and only one of them can sign
    in."""
    db, users = _db({"user_id": "USR-9", "usertype": 1, "name": "A", "status": "active"})

    find_principal_by_email(db, address)

    assert users.calls[0]["filter"] == {"email": "usr-9@example.test"}


def test_the_lookup_is_an_exact_match_never_a_pattern():
    """A regex here would turn "prove you own this address" into "name any address that looks a
    bit like one"."""
    db, users = _db(None)

    find_principal_by_email(db, "usr-9@example.test")

    assert users.calls[0]["filter"] == {"email": "usr-9@example.test"}
    assert "$regex" not in str(users.calls[0]["filter"])


def test_only_the_fields_needed_are_read():
    """It reads a denied field; the narrower the shape that comes back, the less there is to leak
    by accident."""
    db, users = _db(None)

    find_principal_by_email(db, "a@b.test")

    assert users.calls[0]["projection"] == {
        "_id": 0,
        "user_id": 1,
        "usertype": 1,
        "name": 1,
        "status": 1,
    }


# --- everything that must come back as "no" ----------------------------------------------------


def test_an_unknown_address_resolves_to_nothing():
    db, _ = _db(None)

    assert find_principal_by_email(db, "nobody@example.test") is None


@pytest.mark.parametrize("status", ["suspended", "inactive", "", "ACTIVE_PENDING"])
def test_an_account_that_cannot_sign_in_resolves_to_nothing(status):
    """A suspended vendor keeping their data access until their token expires is exactly the
    offboarding hole that makes role-based access theatre."""
    db, _ = _db({"user_id": "USR-9", "usertype": 2, "name": "X", "status": status})

    assert find_principal_by_email(db, "a@b.test") is None


def test_an_unmapped_usertype_resolves_to_nothing():
    """Better to refuse a sign-in than to guess a role for a discriminator value nobody has
    decided the meaning of."""
    db, _ = _db({"user_id": "USR-9", "usertype": 7, "name": "X", "status": "active"})

    assert find_principal_by_email(db, "a@b.test") is None


def test_a_row_without_a_user_id_resolves_to_nothing():
    db, _ = _db({"usertype": 1, "name": "X", "status": "active"})

    assert find_principal_by_email(db, "a@b.test") is None


def test_an_empty_address_never_reaches_the_database():
    db, users = _db(None)

    assert find_principal_by_email(db, "   ") is None
    assert users.calls == []


def test_normalize_email_is_lowercase_and_trimmed():
    assert normalize_email("  A@B.Test ") == "a@b.test"


# --- re-checking an asserted identity ----------------------------------------------------------


def test_a_live_account_of_the_right_role_exists():
    db, users = _db({"status": "active"})

    assert principal_exists(db, "USR-31", role=Role.VENDOR) is True
    assert users.calls[0]["filter"] == {"user_id": "USR-31", "usertype": 2}


def test_a_suspended_account_does_not_exist_for_sign_in():
    db, _ = _db({"status": "suspended"})

    assert principal_exists(db, "USR-31", role=Role.VENDOR) is False


def test_an_operator_account_is_checked_like_any_other():
    """An admin *may* have a row (usertype 3, so they can sign in with a password) or may be
    asserted by a host application with no row at all. Both are legitimate, which is why
    `app/api/server.py` skips this check for admins rather than relying on its answer."""
    db, users = _db({"status": "active"})

    assert principal_exists(db, "USR-51", role=Role.ADMIN) is True
    assert users.calls[0]["filter"] == {"user_id": "USR-51", "usertype": 3}


def test_an_asserted_admin_with_no_account_does_not_exist():
    db, _ = _db(None)

    assert principal_exists(db, "ops-1", role=Role.ADMIN) is False


def test_an_anonymous_role_maps_to_no_account_at_all():
    """ANONYMOUS has no usertype, so there is nothing to look up and nothing to confirm."""
    db, users = _db({"status": "active"})

    assert principal_exists(db, "whoever", role=Role.ANONYMOUS) is False
    assert users.calls == []


# --- signing in with a password ----------------------------------------------------------------


def test_the_right_password_resolves_to_the_accounts_own_role():
    db, _ = _db(
        {
            "user_id": "USR-9",
            "usertype": 2,
            "name": "Metro Mart",
            "status": "active",
            "password_hash": HASH,
        }
    )

    principal = authenticate_password(db, "USR-9@Example.Test ", PASSWORD)

    assert (principal.role, principal.user_id, principal.display_name) == (
        Role.VENDOR,
        "USR-9",
        "Metro Mart",
    )


def test_an_operator_account_signs_in_as_an_admin():
    """The playground's admin view has to be reachable by somebody, and choosing your own role in
    a form is not authentication."""
    db, _ = _db(
        {
            "user_id": "USR-51",
            "usertype": 3,
            "name": "Operator",
            "status": "active",
            "password_hash": HASH,
        }
    )

    assert authenticate_password(db, "admin@example.test", PASSWORD).role is Role.ADMIN


def test_the_wrong_password_resolves_to_nothing():
    db, _ = _db({"user_id": "USR-9", "usertype": 1, "status": "active", "password_hash": HASH})

    assert authenticate_password(db, "a@b.test", "not it") is None


def test_an_unknown_address_and_a_wrong_password_are_the_same_answer():
    db, _ = _db(None)

    assert authenticate_password(db, "nobody@example.test", PASSWORD) is None


@pytest.mark.parametrize("status", ["suspended", "inactive", ""])
def test_the_right_password_on_a_dead_account_says_the_account_is_dead(status):
    """The password proves who you are; it does not decide whether you may still sign in -- and
    once it has verified, saying *which* of the two failed reveals nothing the person hasn't just
    proved. Folding this into "those don't match" sent someone to re-check a correct password."""
    db, _ = _db({"user_id": "USR-9", "usertype": 1, "status": status, "password_hash": HASH})

    with pytest.raises(AccountNotActiveError):
        authenticate_password(db, "a@b.test", PASSWORD)


@pytest.mark.parametrize("status", ["suspended", "inactive"])
def test_a_closed_account_is_indistinguishable_without_the_password(status):
    """The other half of the same rule, and the load-bearing one. `find_principal_by_email` proves
    nothing, so it must stay a single undifferentiated `None` -- otherwise "is this address
    registered?" is answerable with no credential at all."""
    db, _ = _db({"user_id": "USR-9", "usertype": 1, "status": status, "password_hash": HASH})

    assert find_principal_by_email(db, "a@b.test") is None


def test_a_wrong_password_on_a_closed_account_reveals_nothing():
    """Order matters: the status check is *after* the password check, so a guess at a closed
    account's address gets the same nothing as a guess at any other."""
    db, _ = _db({"user_id": "USR-9", "usertype": 1, "status": "suspended", "password_hash": HASH})

    assert authenticate_password(db, "a@b.test", "wrong") is None


def test_an_account_with_no_password_cannot_be_signed_into():
    """A missing hash means nobody set a password, not that any password will do."""
    db, _ = _db({"user_id": "USR-9", "usertype": 1, "status": "active"})

    assert authenticate_password(db, "a@b.test", PASSWORD) is None
    assert authenticate_password(db, "a@b.test", "") is None


def test_the_password_lookup_reads_the_hash_and_nothing_else_new():
    """It reads a redacted field. The projection is the boundary -- widening it to `email`/`phone`
    would hand a sign-in path a contact record it has no use for."""
    db, users = _db(None)

    authenticate_password(db, "a@b.test", PASSWORD)

    assert users.calls[0]["projection"] == {
        "_id": 0,
        "user_id": 1,
        "usertype": 1,
        "name": 1,
        "status": 1,
        "password_hash": 1,
    }


def test_the_password_lookup_is_an_exact_match_never_a_pattern():
    db, users = _db(None)

    authenticate_password(db, "a@b.test", PASSWORD)

    assert users.calls[0]["filter"] == {"email": "a@b.test"}


def test_an_empty_field_never_reaches_the_database():
    db, users = _db(None)

    assert authenticate_password(db, "   ", PASSWORD) is None
    assert authenticate_password(db, "a@b.test", "") is None
    assert users.calls == []


# --- contact lookup for a report -----------------------------------------------------------


class _FakeUsersWithFind(_FakeUsers):
    """Adds `find`, which fetch_contacts uses. Returns whatever rows it was built with."""

    def __init__(self, rows):
        super().__init__(None)
        self._rows = rows
        self.find_calls = []

    def find(self, filter_, projection=None):
        self.find_calls.append({"filter": filter_, "projection": projection})
        return iter(self._rows)


def test_contacts_are_returned_keyed_by_user_id():
    users = _FakeUsersWithFind(
        [
            {"user_id": "USR-1", "email": "a@b.test", "phone": "+92300"},
            {"user_id": "USR-2", "email": "c@d.test"},
        ]
    )

    contacts = fetch_contacts(_FakeDb(users), ["USR-1", "USR-2"])

    assert contacts == {
        "USR-1": {"email": "a@b.test", "phone": "+92300"},
        "USR-2": {"email": "c@d.test"},
    }


def test_the_contact_lookup_is_a_plain_id_fetch():
    """Deliberately dumber than the sign-in lookup: no matching, no searching, no filtering. It
    reads columns for ids the caller has already established the principal may see."""
    users = _FakeUsersWithFind([])

    fetch_contacts(_FakeDb(users), ["USR-1", "USR-1", "USR-2"])

    assert users.find_calls[0]["filter"] == {"user_id": {"$in": ["USR-1", "USR-2"]}}
    assert users.find_calls[0]["projection"] == {"_id": 0, "user_id": 1, "email": 1, "phone": 1}


def test_no_ids_means_no_query():
    users = _FakeUsersWithFind([])

    assert fetch_contacts(_FakeDb(users), []) == {}
    assert fetch_contacts(_FakeDb(users), [None, ""]) == {}
    assert users.find_calls == []
