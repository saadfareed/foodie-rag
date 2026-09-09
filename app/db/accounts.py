"""Creating and repairing sign-in credentials on the `users` collection.

**This is the only module in `app/` that writes to MongoDB, and it is deliberately not on the
question path.** Everything else here reads: `MONGODB_URI` is documented as a read-only
credential, `app/db/executor.py` runs validated read queries and nothing else, and that is a
guardrail worth keeping exactly as it is. So the write lives in one file, is called from exactly
two places -- the dev playground's sign-up form and the `backfill_credentials` CLI -- and both
are things an operator switches on deliberately.

If `MONGODB_URI` is read-only (it should be, in production), these functions raise and their
callers say so plainly. That is the correct outcome, not a bug: a deployment answering questions
has no business creating accounts, and the failure is loud rather than silent.

What a sign-in needs, and therefore what this writes:

    user_id        generated, `USR-#####`, continuing the existing sequence
    email          normalised and unique -- `app/db/indexes.py` enforces that with a partial
                   unique index, so a duplicate is refused by the database, not just by a check
    password_hash  PBKDF2 via app/security/passwords.py; the plaintext is never stored or logged
    usertype       from the role (app/security/roles.py::usertype_for) -- never from a form field
    status         "active"; anything else cannot sign in (app/db/identity.py)

Plus the per-domain fields a report expects to find (`business_name`/`category`/`rating` for a
vendor, `loyalty_tier` for a customer), because an account that exists but renders as blank
columns in every report is a worse demo than no account at all.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any

from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from app.db.identity import normalize_email
from app.security.passwords import hash_password
from app.security.roles import Role, usertype_for

logger = logging.getLogger("audit")

#: The id format the seeded data uses (`USR-00031`). New accounts continue the same sequence so
#: one collection doesn't end up with two id conventions in it.
_USER_ID = re.compile(r"^USR-(\d+)$")
_USER_ID_TEMPLATE = "USR-{:05d}"

#: Enough to be worth hashing, short enough not to argue with a demo password. The real bound on
#: a password's usefulness here is that these are throwaway accounts on a dev playground.
MIN_PASSWORD_LENGTH = 6

#: Rough shape check only. Verifying an address means sending something to it -- which is the
#: host application's job (see docs/authorization.md), not this form's.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AccountError(Exception):
    """A sign-up that can't proceed, carrying text meant for the person who filled the form.

    Distinct from the credential failures in `app/messages.py`: those are deliberately identical
    for every cause so a login form can't be used to test whether an address is registered. A
    *sign-up* form has the opposite requirement -- "that address is already taken" is the one
    thing the person needs to know, and refusing to say it makes the form unusable.
    """


def next_user_id(db: Database) -> str:
    """The next id in the `USR-#####` sequence.

    Reads the current maximum rather than counting documents: a collection someone has deleted
    from would otherwise reissue an id that orders still reference, silently reattributing them.
    """
    highest = 0
    for row in db["users"].find({"user_id": {"$regex": r"^USR-\d+$"}}, {"_id": 0, "user_id": 1}):
        match = _USER_ID.match(str(row.get("user_id", "")))
        if match:
            highest = max(highest, int(match.group(1)))
    return _USER_ID_TEMPLATE.format(highest + 1)


def validate_signup(
    *, name: str, email: str, password: str, role: Role, allowed_roles: set[str]
) -> None:
    """Raise `AccountError` if this sign-up can't be accepted. Says which field and why."""
    if not name.strip():
        raise AccountError("Enter a name.")
    if not _EMAIL.match(normalize_email(email)):
        raise AccountError("Enter a valid email address.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AccountError(f"Use a password of at least {MIN_PASSWORD_LENGTH} characters.")
    if role is Role.ANONYMOUS or role.value not in allowed_roles:
        # The same list that governs which roles a session may be minted for. Letting someone
        # sign up as a role the gateway would then refuse to answer for is a form that produces
        # an account you cannot use.
        raise AccountError("That account type isn't enabled here.")


def create_account(
    db: Database,
    *,
    name: str,
    email: str,
    password: str,
    role: Role,
    city: str = "",
    location: dict | None = None,
    business_name: str = "",
    category: str = "",
) -> dict:
    """Insert one account and return it (without the hash), or raise `AccountError`.

    The uniqueness of `email` is enforced by the database's partial unique index, and this catches
    `DuplicateKeyError` rather than checking first: a read-then-write check is a race, and the
    index has to exist anyway for the sign-in lookup to be fast.
    """
    usertype = usertype_for(role)
    if usertype is None:
        raise AccountError("That account type isn't enabled here.")

    now = datetime.now(timezone.utc)
    document: dict[str, Any] = {
        "user_id": next_user_id(db),
        "name": name.strip(),
        "email": normalize_email(email),
        "password_hash": hash_password(password),
        "usertype": usertype,
        "status": "active",
        "city": city.strip(),
        "created_at": now,
        "last_active_at": now,
    }
    if location:
        document["location"] = location
    if role is Role.VENDOR:
        # A vendor with no business name renders as a blank column in every report that lists
        # vendors (app/agents/domains.py::report_columns), so fall back to their own name.
        document["business_name"] = business_name.strip() or name.strip()
        document["category"] = category.strip() or "general"
        document["rating"] = 0.0
    elif role is Role.CUSTOMER:
        document["loyalty_tier"] = "bronze"

    try:
        db["users"].insert_one(document)
    except DuplicateKeyError as exc:
        raise AccountError("An account with that email already exists. Sign in instead.") from exc

    logger.info(
        "account_created",
        # The address and the hash are both deliberately absent: this line ends up in the same
        # audit log as every answered question, and neither belongs there.
        extra={"event": {"user_id": document["user_id"], "role": role.value}},
    )
    return {key: value for key, value in document.items() if key != "password_hash"}
