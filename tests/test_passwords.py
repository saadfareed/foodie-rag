"""Password hashing (app/security/passwords.py).

Two properties matter more than the rest, and neither is visible by reading a stored value: that
the same password hashes differently every time (so one cracked hash is not a lookup table), and
that a failure costs the same as a success (so the sign-in endpoint in front of this cannot be
asked whether an address is registered).
"""

import time

import pytest

from app.security.passwords import DEFAULT_ITERATIONS, hash_password, verify_password

PASSWORD = "correct horse battery staple"


def test_the_right_password_verifies():
    assert verify_password(PASSWORD, hash_password(PASSWORD, iterations=1)) is True


def test_the_wrong_password_does_not():
    assert verify_password("nearly right", hash_password(PASSWORD, iterations=1)) is False


def test_the_stored_value_is_not_the_password():
    encoded = hash_password(PASSWORD, iterations=1)

    assert PASSWORD not in encoded
    assert encoded.startswith("pbkdf2_sha256$1$")


def test_the_same_password_hashes_differently_every_time():
    """A per-record salt. Without it two accounts sharing a password share a hash, and cracking
    one cracks every other."""
    first = hash_password(PASSWORD, iterations=1)
    second = hash_password(PASSWORD, iterations=1)

    assert first != second
    assert verify_password(PASSWORD, first) and verify_password(PASSWORD, second)


def test_a_hash_carries_its_own_cost_factor():
    """Which is what lets the iteration count be raised later without invalidating every existing
    account -- an old hash keeps verifying at the count it was written with."""
    cheap = hash_password(PASSWORD, iterations=1)
    dearer = hash_password(PASSWORD, iterations=2)

    assert verify_password(PASSWORD, cheap)
    assert verify_password(PASSWORD, dearer)
    assert cheap.split("$")[1] != dearer.split("$")[1]


@pytest.mark.parametrize(
    "stored",
    [
        None,
        "",
        "not-a-hash",
        "pbkdf2_sha256$only$three",
        "bcrypt$1$c2FsdA$ZGlnZXN0",  # an algorithm this module does not implement
        "pbkdf2_sha256$0$c2FsdA$ZGlnZXN0",  # zero iterations
        "pbkdf2_sha256$1$$ZGlnZXN0",  # no salt
        "pbkdf2_sha256$1$c2FsdA$",  # no digest
        "pbkdf2_sha256$abc$c2FsdA$ZGlnZXN0",  # unparseable cost factor
    ],
)
def test_a_record_that_is_not_a_usable_hash_never_verifies(stored):
    """Every malformed shape is False, never an exception and never True. A crash here would be a
    500 on a login form, which is itself a signal about the account."""
    assert verify_password(PASSWORD, stored) is False


def test_an_empty_password_cannot_be_hashed():
    """Refused rather than hashed: a stored hash of "" is an account anyone can sign into by
    leaving the field blank."""
    with pytest.raises(ValueError):
        hash_password("")


def test_a_missing_hash_costs_what_a_real_one_does():
    """The timing half of "an unknown address and a wrong password are the same answer". Measured
    generously -- this asserts the dummy derivation happens at all, not a precise duration.
    """
    real = hash_password(PASSWORD, iterations=DEFAULT_ITERATIONS)

    start = time.perf_counter()
    verify_password(PASSWORD, real)
    against_a_real_hash = time.perf_counter() - start

    start = time.perf_counter()
    verify_password(PASSWORD, None)
    against_nothing = time.perf_counter() - start

    assert against_nothing > against_a_real_hash / 10
