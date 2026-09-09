"""Resolving a sign-in -- an address, or an address and a password -- to the identity a question
is answered under.

This is the one place in the codebase that reads fields `app/security/field_policy.py` denies.
That is deliberate and it is why it lives here rather than anywhere near the agent graph:
`users.email` and `users.password_hash` exist so a person can prove who they are, and these
functions are the single code path allowed to look at them. Everything the model touches goes
through `app/db/executor.py`, which sanitizes on the way out, so no question can reach this data
no matter how it is phrased.

The lookup is by **exact, normalised email**, never a regex or a partial match. A pattern match
here would turn "prove you own this address" into "name any address that looks a bit like one".

Two entry points, differing only in what has already been proven:

* `find_principal_by_email` trusts the caller to have verified the address (a host application's
  own login, a one-time code). It authenticates nobody.
* `authenticate_password` *is* the proof: it checks the password against `users.password_hash`
  (`app/security/passwords.py`). Every refusal -- unknown address, wrong password, suspended
  account, unmapped usertype -- returns the same `None` and costs the same key derivation, so the
  endpoint in front of it cannot be turned into a "does this address exist" oracle.

Read-only, like every other query this application makes -- `MONGODB_URI` is a read-only
credential and nothing here writes. OTP state is not stored in Mongo at all; it lives in
`app/state` (see the host application's sign-in flow), which is also what makes it work across
replicas.
"""

import logging

from pymongo.database import Database

from app.security.passwords import verify_password
from app.security.roles import USERTYPE_ROLES, Principal, Role, usertype_for

logger = logging.getLogger("audit")


class AccountNotActiveError(Exception):
    """The password was right; the account is not one that may sign in.

    Deliberately distinguishable from a failed sign-in, and safe to be so, because of *when* it is
    raised: `authenticate_password` verifies the password first, so reaching this means the caller
    already proved they hold the credential. Telling them their own account is suspended reveals
    nothing they hadn't just demonstrated they were entitled to know.

    That is the whole reason the ordering in `authenticate_password` matters. Checking the status
    first and reporting it would be an enumeration oracle -- "is this address registered and
    closed?" answerable without any password at all. `find_principal_by_email`, which proves
    nothing, still returns a single undifferentiated `None` for every case.
    """


#: Only these accounts may sign in. A suspended vendor keeping their data access until their token
#: expires is precisely the offboarding hole that makes role-based access theatre.
_SIGN_IN_STATUSES = frozenset({"active"})


def normalize_email(email: str) -> str:
    return email.strip().lower()


#: An explicit projection, not a filter applied afterwards: these reads touch denied fields, and
#: the narrower the shape that comes back the less there is to leak by accident.
_IDENTITY_PROJECTION = {"_id": 0, "user_id": 1, "usertype": 1, "name": 1, "status": 1}
#: The password path needs one field more, and nothing else. It is never widened to include
#: `email`/`phone`: the address is already known (it is what was looked up).
_PASSWORD_PROJECTION = {**_IDENTITY_PROJECTION, "password_hash": 1}  # nosec B105 - a projection, not a password


def _is_active(row: dict) -> bool:
    """Whether this account may sign in at all. One definition, used by both paths -- they differ
    only in whether they may *say* which of the two answers it was."""
    status = str(row.get("status", "")).lower()
    if status in _SIGN_IN_STATUSES:
        return True
    logger.info(
        "identity_sign_in_refused",
        extra={"event": {"reason": "account_status", "status": status}},
    )
    return False


def _principal_from_row(row: dict, tenant_id: str) -> Principal | None:
    """The identity a `users` row denotes, or None if it denotes none we can act on.

    Shared by both sign-in paths so that "which accounts may sign in" is decided once. Two
    situations end here: an account that isn't active, and a `usertype` this application has no
    role for -- both refused, because guessing either one wrong grants access.
    """
    if not _is_active(row):
        return None

    role = USERTYPE_ROLES.get(row.get("usertype"))
    user_id = row.get("user_id")
    if role is None or not user_id:
        logger.warning(
            "identity_unmapped_usertype",
            extra={"event": {"usertype": row.get("usertype"), "has_user_id": bool(user_id)}},
        )
        return None

    return Principal(
        role=role, user_id=str(user_id), tenant_id=tenant_id, display_name=row.get("name")
    )


def find_principal_by_email(
    db: Database, email: str, *, tenant_id: str = "default"
) -> Principal | None:
    """The Principal for an **already verified** email address, or None.

    This authenticates nobody -- proving control of the address is the caller's job (see
    `app/api/server.py::identity_lookup`). `authenticate_password` is the entry point that does
    the proving itself.

    None covers three different situations on purpose -- no such address, an address on a
    suspended/inactive account, and an account whose `usertype` maps to no known role. The caller
    must not distinguish them to the user either: any difference in the response is a way to test
    whether an address is registered.
    """
    normalized = normalize_email(email)
    if not normalized:
        return None

    row = db["users"].find_one({"email": normalized}, _IDENTITY_PROJECTION)
    if not row:
        return None

    return _principal_from_row(row, tenant_id)


def authenticate_password(
    db: Database, email: str, password: str, *, tenant_id: str = "default"
) -> Principal | None:
    """The Principal for an address **and** the password that proves it, or None.

    The password is checked before anything else, and an address with no row still pays for a key
    derivation (`verify_password` handles a missing hash by hashing anyway). Both are for the same
    reason: an unknown address and a wrong password must be indistinguishable in response *and* in
    timing, or sign-in becomes the account enumeration tool that `find_principal_by_email` is
    careful not to be.

    An account that *is* closed raises `AccountNotActiveError` rather than joining them. Once the
    password has verified, the caller has proved they hold the credential, so naming the reason
    tells them nothing they hadn't already established -- and folding it into "those don't match"
    sends someone to check a password that was correct all along. That was not hypothetical: a
    demo database with a realistic spread of statuses had 22 of 50 accounts silently unusable,
    every one of them reporting a credential problem it did not have.

    An account with no `password_hash` cannot sign in this way. That is the safe direction: a
    missing hash means nobody has set a password, not that any password will do.
    """
    normalized = normalize_email(email)
    if not normalized or not password:
        # Nothing to check and nothing to leak -- an empty field is the user's own typo, not a
        # probe, and there is no account it could be confused with.
        return None

    row = db["users"].find_one({"email": normalized}, _PASSWORD_PROJECTION) or {}
    if not verify_password(password, row.get("password_hash")):
        logger.info(
            "identity_password_refused",
            extra={"event": {"reason": "no_account" if not row else "bad_password"}},
        )
        return None

    # Past this line the credential is proven, which is what makes the distinction below safe.
    if not _is_active(row):
        raise AccountNotActiveError(str(row.get("status", "")).lower())

    return _principal_from_row(row, tenant_id)


def principal_exists(db: Database, user_id: str, *, role: Role) -> bool:
    """Whether `user_id` is still a live account of `role`.

    Used when a host application asserts an identity directly rather than through an email
    lookup -- the assertion is trusted for *who*, but whether that account is still active is this
    system's own question to answer, and a stale assertion is how a removed vendor keeps reading.
    """
    usertype = usertype_for(role)
    if usertype is None:
        return False
    row = db["users"].find_one({"user_id": user_id, "usertype": usertype}, {"_id": 0, "status": 1})
    return bool(row) and str(row.get("status", "")).lower() in _SIGN_IN_STATUSES


#: Contact columns a report may carry, and the header each becomes.
CONTACT_FIELDS = {"email": "email", "phone": "phone"}


def fetch_contacts(db: Database, user_ids: list[str]) -> dict[str, dict[str, str]]:
    """Contact details for `user_ids`, as `{user_id: {"email": ..., "phone": ...}}`.

    The second code path allowed to read these fields, and it is deliberately dumber than the
    first: no matching, no searching, no filtering -- it takes ids the caller has *already*
    established the principal may see and reads the columns for exactly those.

    Bounded by the caller's row set, which is itself bounded by the report row cap, so this is one
    indexed `$in` over ids that were already fetched. Returns an empty mapping for an empty input
    rather than querying for nothing.
    """
    ids = [uid for uid in dict.fromkeys(user_ids) if uid]
    if not ids:
        return {}

    projection = {"_id": 0, "user_id": 1, **dict.fromkeys(CONTACT_FIELDS, 1)}
    rows = db["users"].find({"user_id": {"$in": ids}}, projection)
    return {
        row["user_id"]: {field: row[field] for field in CONTACT_FIELDS if row.get(field)}
        for row in rows
        if row.get("user_id")
    }
