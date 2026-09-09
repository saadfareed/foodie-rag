"""Hashing and checking a sign-in password.

A password is the one credential this system stores rather than merely receives, so two rules
shape everything here:

1. **The stored value is never the password.** `users.password_hash` holds a PBKDF2-HMAC-SHA256
   digest with a per-user random salt, in a self-describing string that carries its own iteration
   count. Carrying the cost factor in the record is what makes it raisable later without
   invalidating every existing account -- an old hash keeps verifying at its own iteration count
   until that user next sets a password.
2. **A wrong password and an unknown account must cost the same.** `verify_password` does the
   full derivation against a dummy hash when handed nothing, so "no such address" and "wrong
   password" take the same time. Without it the sign-in endpoint answers, in milliseconds,
   whether an address is registered -- which is the same enumeration oracle
   `app/db/identity.py` is careful not to be.

Standard library only, deliberately. `hashlib.pbkdf2_hmac` is a NIST-recommended KDF present in
every Python this project supports; adding bcrypt/argon2 would mean a compiled dependency in the
Slack-only deployment too, for a demo-scale sign-in.

Nothing in the request path reads `password_hash` -- `app/security/field_policy.py` matches it as
a secret field and replaces the value before a row leaves `app/db/executor.py`, so no question can
be phrased that reveals one. `app/db/identity.py` is the only code path that reads it at all.
"""

import base64
import binascii
import functools
import hashlib
import hmac
import secrets

_ALGORITHM = "pbkdf2_sha256"
#: Cost factor for a *new* hash. Existing hashes verify at whatever count they were written with.
#: Chosen so one sign-in costs a fraction of a second on a laptop and seeding fifty demo accounts
#: stays a few seconds; raise it as hardware moves, and old accounts keep working.
DEFAULT_ITERATIONS = 200_000
_SALT_BYTES = 16


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _derive(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def hash_password(password: str, *, iterations: int = DEFAULT_ITERATIONS) -> str:
    """`algorithm$iterations$salt$digest`, all of it safe to store.

    A fresh random salt per call, so two users choosing the same password do not share a hash --
    which is what stops one cracked password from being a lookup table for the rest.
    """
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _derive(password, salt, iterations)
    return f"{_ALGORITHM}${iterations}${_b64(salt)}${_b64(digest)}"


@functools.lru_cache(maxsize=1)
def dummy_hash() -> str:
    """A real hash of a password nobody knows, used only to spend the same time on an account
    that doesn't exist. Built on first use rather than at import so merely importing this module
    doesn't cost a key derivation, and cached so every probe costs the same after that."""
    return hash_password(secrets.token_urlsafe(32))


def verify_password(password: str, encoded: str | None) -> bool:
    """Whether `password` matches `encoded`.

    `None`, an empty string and a malformed record all verify against `dummy_hash()` and return
    False, rather than returning early: an account with no password set must not be cheaper to
    probe than one with a password. Comparison is `compare_digest`, so a near-miss doesn't leak
    how near it was.
    """
    parts = (encoded or "").split("$")
    if len(parts) != 4 or parts[0] != _ALGORITHM:
        _spend_dummy_work(password)
        return False

    _, iterations_text, salt_text, digest_text = parts
    try:
        iterations = int(iterations_text)
        salt = _unb64(salt_text)
        expected = _unb64(digest_text)
    except (ValueError, binascii.Error):
        _spend_dummy_work(password)
        return False
    if iterations <= 0 or not salt or not expected:
        _spend_dummy_work(password)
        return False

    return hmac.compare_digest(_derive(password, salt, iterations), expected)


def _spend_dummy_work(password: str) -> None:
    """Derive against `dummy_hash()` and throw the answer away. See the module docstring."""
    _, iterations_text, salt_text, _ = dummy_hash().split("$")
    _derive(password, _unb64(salt_text), int(iterations_text))
