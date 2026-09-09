"""Backfilling `email`/`password_hash` onto existing users (app/db/backfill_credentials.py).

A bulk write over a collection that may hold real people, so the properties that matter are the
ones about restraint: it must not overwrite what is already there, must not invent an address that
could reach a real inbox, and must not write at all unless asked twice.
"""

from app.db.backfill_credentials import DEFAULT_EMAIL_DOMAIN, plan_backfill

ROWS = [
    {"user_id": "USR-00001"},  # needs both
    {"user_id": "USR-00002", "email": "real@person.example"},  # needs a password only
    {"user_id": "USR-00003", "password_hash": "pbkdf2_sha256$1$x$y"},  # needs an email only
    {"user_id": "USR-00004", "email": "a@b.test", "password_hash": "pbkdf2_sha256$1$x$y"},  # done
]


def _by_id(planned):
    return {p["user_id"]: p for p in planned}


def test_only_rows_that_need_something_are_planned():
    planned = _by_id(plan_backfill(ROWS))

    assert set(planned) == {"USR-00001", "USR-00002", "USR-00003"}


def test_an_existing_address_is_never_replaced():
    """An address is identity. Overwriting one with a generated address quietly reassigns who the
    account belongs to."""
    planned = _by_id(plan_backfill(ROWS))

    assert planned["USR-00002"]["email"] is None
    assert planned["USR-00002"]["password"] is True


def test_an_existing_password_is_never_reset_by_default():
    planned = _by_id(plan_backfill(ROWS))

    assert planned["USR-00003"]["password"] is False
    assert planned["USR-00003"]["email"] == f"usr-00003@{DEFAULT_EMAIL_DOMAIN}"


def test_resetting_passwords_is_opt_in():
    planned = _by_id(plan_backfill(ROWS, reset_passwords=True))

    assert planned["USR-00003"]["password"] is True
    assert planned["USR-00004"]["password"] is True


def test_generated_addresses_cannot_reach_a_real_inbox():
    """`example.test` is reserved by RFC 6761 and cannot resolve -- which matters when the
    generation runs over a collection of real customers."""
    planned = _by_id(plan_backfill(ROWS))

    assert planned["USR-00001"]["email"].endswith("@example.test")


def test_a_real_domain_can_be_supplied():
    planned = _by_id(plan_backfill(ROWS, email_domain="corp.example"))

    assert planned["USR-00001"]["email"] == "usr-00001@corp.example"


def test_a_row_with_no_user_id_is_skipped():
    """There is nothing to key an address on, and no way to sign in as a row that isn't
    identified -- so it is left exactly as found rather than guessed at."""
    assert plan_backfill([{"name": "No id"}, {"user_id": "  "}]) == []


def test_the_plan_carries_no_password():
    """The hash is generated per row at write time, so each account keeps its own salt. A plan
    holding one password would be a plan holding one salt for the whole collection."""
    for change in plan_backfill(ROWS):
        assert set(change) == {"user_id", "email", "password"}
        assert isinstance(change["password"], bool)
