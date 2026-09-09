"""Creating an account (app/db/accounts.py).

This is the only module in `app/` that writes to MongoDB, so the tests are as much about what it
refuses to write as what it writes: a role nobody may hold, an address already taken, an id that
collides with one an order still references.
"""

import pytest
from pymongo.errors import DuplicateKeyError

from app.db.accounts import (
    MIN_PASSWORD_LENGTH,
    AccountError,
    create_account,
    next_user_id,
    validate_signup,
)
from app.security.passwords import verify_password
from app.security.roles import Role

ALLOWED = {"customer", "vendor"}


class _FakeUsers:
    """Records what it was asked to insert. `duplicate` makes the next insert raise the way a
    unique index does."""

    def __init__(self, rows=None, duplicate=False):
        self._rows = rows or []
        self.duplicate = duplicate
        self.inserted = []

    def find(self, filter_, projection=None):
        return iter(self._rows)

    def insert_one(self, document):
        if self.duplicate:
            raise DuplicateKeyError("E11000 duplicate key error: email")
        self.inserted.append(document)
        return object()


class _FakeDb:
    def __init__(self, users):
        self._users = users

    def __getitem__(self, name):
        assert name == "users"
        return self._users


def _db(rows=None, duplicate=False):
    users = _FakeUsers(rows, duplicate)
    return _FakeDb(users), users


# --- generated ids ------------------------------------------------------------------------------


def test_the_next_id_continues_the_existing_sequence():
    db, _ = _db([{"user_id": "USR-00001"}, {"user_id": "USR-00007"}, {"user_id": "USR-00003"}])

    assert next_user_id(db) == "USR-00008"


def test_the_first_id_in_an_empty_collection():
    db, _ = _db([])

    assert next_user_id(db) == "USR-00001"


def test_the_next_id_is_the_maximum_not_the_count():
    """Counting would reissue an id that orders still reference the moment anyone deletes a row,
    silently reattributing that history to a new person."""
    db, _ = _db([{"user_id": "USR-00009"}])

    assert next_user_id(db) == "USR-00010"


def test_ids_in_another_format_are_ignored():
    db, _ = _db([{"user_id": "legacy-3"}, {"user_id": "USR-00002"}])

    assert next_user_id(db) == "USR-00003"


# --- what may be signed up ----------------------------------------------------------------------


def test_a_valid_signup_passes():
    validate_signup(
        name="Ayesha Khan",
        email="a@b.test",
        password="test123",
        role=Role.CUSTOMER,
        allowed_roles=ALLOWED,
    )


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("name", "   ", "name"),
        ("email", "not-an-email", "email"),
        ("password", "12345", str(MIN_PASSWORD_LENGTH)),
    ],
)
def test_a_bad_field_is_named_in_the_error(field, value, expected):
    """A sign-up form's errors name the field, unlike a sign-in's -- there is no account to
    confirm the existence of yet, and "that didn't work" leaves someone guessing which box."""
    kwargs = {
        "name": "Ayesha Khan",
        "email": "a@b.test",
        "password": "test123",
        "role": Role.CUSTOMER,
        "allowed_roles": ALLOWED,
    }
    kwargs[field] = value

    with pytest.raises(AccountError, match=expected):
        validate_signup(**kwargs)


def test_a_role_the_gateway_would_refuse_cannot_be_signed_up_for():
    """`admin` is not in the default WIDGET_ALLOWED_SESSION_ROLES. Creating an account in a role
    the gateway then won't mint a session for produces an account nobody can use."""
    with pytest.raises(AccountError):
        validate_signup(
            name="Ops", email="a@b.test", password="test123", role=Role.ADMIN, allowed_roles=ALLOWED
        )


def test_anonymous_is_not_an_account_type():
    with pytest.raises(AccountError):
        validate_signup(
            name="Nobody",
            email="a@b.test",
            password="test123",
            role=Role.ANONYMOUS,
            allowed_roles=ALLOWED | {"anonymous"},
        )


# --- what gets written ---------------------------------------------------------------------------


def test_a_customer_is_written_with_the_fields_a_customer_needs():
    db, users = _db([])

    account = create_account(
        db,
        name="Ayesha Khan",
        email="  Ayesha@Example.Test ",
        password="test123",
        role=Role.CUSTOMER,
        city="Karachi",
    )

    written = users.inserted[0]
    assert written["usertype"] == 1
    assert written["email"] == "ayesha@example.test", "normalised, or sign-in can't find it"
    assert written["status"] == "active"
    assert written["loyalty_tier"] == "bronze"
    assert "business_name" not in written
    assert account["user_id"] == "USR-00001"


def test_a_vendor_gets_the_columns_a_vendor_report_reads():
    db, users = _db([])

    create_account(
        db,
        name="Sana Mirza",
        email="s@example.test",
        password="test123",
        role=Role.VENDOR,
        business_name="Metro Grocers",
        category="grocery",
    )

    written = users.inserted[0]
    assert written["usertype"] == 2
    assert (written["business_name"], written["category"]) == ("Metro Grocers", "grocery")
    assert "loyalty_tier" not in written


def test_a_vendor_without_a_business_name_falls_back_to_their_own():
    """A blank business_name renders as an empty column in every report that lists vendors."""
    db, users = _db([])

    create_account(
        db, name="Sana Mirza", email="s@example.test", password="test123", role=Role.VENDOR
    )

    assert users.inserted[0]["business_name"] == "Sana Mirza"


def test_the_password_is_stored_only_as_a_hash():
    db, users = _db([])

    account = create_account(
        db, name="A B", email="a@b.test", password="test123", role=Role.CUSTOMER
    )

    written = users.inserted[0]
    assert "test123" not in str(written)
    assert verify_password("test123", written["password_hash"])
    assert "password_hash" not in account, "the caller gets the account back, never the credential"


def test_a_location_is_stored_as_a_geojson_point():
    db, users = _db([])

    create_account(
        db,
        name="A B",
        email="a@b.test",
        password="test123",
        role=Role.CUSTOMER,
        location={"type": "Point", "coordinates": [74.3587, 31.5204]},
    )

    assert users.inserted[0]["location"]["type"] == "Point"


def test_no_location_writes_no_location_field():
    """Absent rather than null, matching how every other optional field in this data behaves."""
    db, users = _db([])

    create_account(db, name="A B", email="a@b.test", password="test123", role=Role.CUSTOMER)

    assert "location" not in users.inserted[0]


def test_a_taken_address_is_refused_by_the_database_not_a_prior_check():
    """Checking first and inserting second is a race two sign-ups can both win. The partial unique
    index in app/db/indexes.py is the thing that actually holds."""
    db, _ = _db([], duplicate=True)

    with pytest.raises(AccountError, match="already exists"):
        create_account(db, name="A B", email="a@b.test", password="test123", role=Role.CUSTOMER)
