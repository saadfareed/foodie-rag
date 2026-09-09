"""Give existing `users` rows the fields a sign-in needs: `email` and `password_hash`.

The demo seeder writes both, but a `users` collection that predates them -- or one holding real
data that was never meant to be signed into -- has neither, and every sign-in against it fails
with the same "that email and password don't match" it would give for a wrong password. This is
the one-off that fixes that.

    python -m app.db.backfill_credentials              # report what would change, write nothing
    python -m app.db.backfill_credentials --apply      # actually write

**A dry run by default**, because this is a bulk write over a collection that may hold real
people. It prints the counts and three example rows first; `--apply` is a second, deliberate act.
It also needs a *writable* `MONGODB_URI`, which production deliberately does not have (see
app/db/accounts.py) -- run it with an admin credential, once, and put the read-only one back.

It also prints accounts you can actually **sign in with**, because a generated address is only
half the answer: `app/db/identity.py` refuses any account whose `status` isn't `active`, and demo
data deliberately contains a realistic spread of suspended and inactive ones. Being handed 50
addresses of which 22 are refused is how "the password isn't working" happens.

    python -m app.db.backfill_credentials --apply --activate    # ...and make them all active

What it will not do:

* **Overwrite an existing `password_hash`.** Someone who has set a password keeps it. Pass
  `--reset-passwords` to deliberately reset everyone to the default, which is a thing you want
  for a demo database and never for a real one.
* **Overwrite an existing `email`.** Addresses are identity; a generated one replacing a real one
  would quietly reassign who an account belongs to.
* **Activate anybody**, unless `--activate` says so. A suspended account is data -- it is what
  makes "how many suspended vendors are there?" a question with a real answer -- so turning the
  whole collection active is a choice, not a repair.
* **Invent an address that could reach a real inbox.** The default domain is `example.test`, a
  reserved TLD that cannot resolve. Pass `--email-domain` if you have a real one to use.
"""

import argparse

from app.db.mongo import get_db
from app.security.passwords import hash_password

#: Reserved by RFC 6761 and guaranteed not to resolve, so a generated address can never reach a
#: real person -- which matters when the generation is over a collection of real customers.
DEFAULT_EMAIL_DOMAIN = "example.test"
DEFAULT_PASSWORD = "test123"  # nosec B105 - the documented default for demo/dev accounts

#: Enough to decide what each row needs, and nothing more. This reads two fields the field policy
#: denies to everything else, so the narrower the shape the less there is to leak by accident.
_BACKFILL_PROJECTION = {  # nosec B105 - a projection, not a password
    "_id": 0,
    "user_id": 1,
    "email": 1,
    "password_hash": 1,
}


def plan_backfill(
    rows: list[dict], *, email_domain: str = DEFAULT_EMAIL_DOMAIN, reset_passwords: bool = False
) -> list[dict]:
    """What would change, as `[{"user_id": ..., "email": <address or None>, "password": bool}]`.

    Pure, so the dry run and the write share one definition of "what this does" rather than the
    dry run being an approximation of it -- a preview that can disagree with the write is worse
    than no preview. It names *whether* a password is needed rather than carrying one: the hash
    is generated per row at write time, so there is nothing to plan and nothing to hold.
    """
    planned = []
    for row in rows:
        user_id = str(row.get("user_id") or "").strip()
        if not user_id:
            # Nothing to key an address on, and no way to sign in as a row that isn't identified.
            continue
        needs_email = not str(row.get("email") or "").strip()
        needs_password = reset_passwords or not str(row.get("password_hash") or "").strip()
        if needs_email or needs_password:
            planned.append(
                {
                    "user_id": user_id,
                    "email": f"{user_id.lower()}@{email_domain}" if needs_email else None,
                    "password": needs_password,
                }
            )
    return planned


def signin_examples(db, limit: int = 3) -> list[dict]:
    """A few accounts per role that can actually sign in, for the summary to print.

    Only `active` ones, because those are the only ones `app/db/identity.py` will accept -- the
    whole point of printing them is to hand over addresses that work.
    """
    examples = []
    for usertype, role in ((1, "customer"), (2, "vendor"), (3, "admin")):
        rows = db["users"].find(
            {"usertype": usertype, "status": "active", "email": {"$type": "string"}},
            {"_id": 0, "email": 1, "name": 1},
        )
        for row in list(rows.limit(limit)):
            examples.append({"role": role, "email": row.get("email"), "name": row.get("name")})
    return examples


def backfill(
    *,
    password: str = DEFAULT_PASSWORD,
    email_domain: str = DEFAULT_EMAIL_DOMAIN,
    reset_passwords: bool = False,
    activate: bool = False,
    apply: bool = False,
) -> dict:
    """Run the backfill (or preview it). Returns a summary for the caller to print."""
    db = get_db()
    rows = list(db["users"].find({}, _BACKFILL_PROJECTION))
    planned = plan_backfill(rows, email_domain=email_domain, reset_passwords=reset_passwords)

    inactive = db["users"].count_documents({"status": {"$ne": "active"}})
    summary = {
        "total": len(rows),
        "inactive": inactive,
        "needing_email": sum(1 for p in planned if p["email"]),
        "needing_password": sum(1 for p in planned if p["password"]),
        "changed": 0,
        "examples": [p["user_id"] for p in planned[:3]],
        "applied": apply,
    }
    if not apply:
        summary["examples_that_can_sign_in"] = signin_examples(db)
        return summary

    if activate:
        # Explicitly opt-in: see the module docstring. Counted separately so the summary can say
        # what it did rather than implying the backfill itself unlocked these.
        summary["activated"] = (
            db["users"]
            .update_many({"status": {"$ne": "active"}}, {"$set": {"status": "active"}})
            .modified_count
        )

    if not planned:
        summary["examples_that_can_sign_in"] = signin_examples(db)
        return summary

    for change in planned:
        update: dict[str, str] = {}
        if change["email"]:
            update["email"] = change["email"]
        if change["password"]:
            # Hashed per row so every account carries its own salt, exactly as the seeder does --
            # one hash copied across a collection would make one cracked password crack them all.
            update["password_hash"] = hash_password(password)
        db["users"].update_one({"user_id": change["user_id"]}, {"$set": update})
        summary["changed"] += 1
    summary["examples_that_can_sign_in"] = signin_examples(db)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--email-domain", default=DEFAULT_EMAIL_DOMAIN)
    parser.add_argument("--reset-passwords", action="store_true")
    parser.add_argument(
        "--activate",
        action="store_true",
        help="also set every account active (only an active account can sign in)",
    )
    parser.add_argument("--apply", action="store_true", help="write; otherwise this is a dry run")
    args = parser.parse_args()

    result = backfill(
        password=args.password,
        email_domain=args.email_domain,
        reset_passwords=args.reset_passwords,
        activate=args.activate,
        apply=args.apply,
    )

    print(f"users in collection:      {result['total']}")
    print(f"missing an email:         {result['needing_email']}")
    print(f"missing a password:       {result['needing_password']}")
    # Printed whether or not anything was backfilled: a correct email and password on a suspended
    # account still cannot sign in, and that is the failure this line exists to pre-empt.
    print(f"cannot sign in (status):  {result['inactive']}")
    if result["examples"]:
        print(f"for example:              {', '.join(result['examples'])}")

    if result["applied"]:
        print(f"\nUpdated {result['changed']} user(s). Every one signs in with: {args.password}")
        print(f"Addresses are <user_id>@{args.email_domain} -- e.g. usr-00031@{args.email_domain}")
        if result.get("activated"):
            print(f"Activated {result['activated']} previously inactive/suspended account(s).")
    elif result["needing_email"] or result["needing_password"]:
        print("\nDry run -- nothing was written. Re-run with --apply to make these changes.")
        print("Note this needs a WRITABLE MONGODB_URI; production's should be read-only.")
    else:
        print("\nEvery user already has an email and a password.")

    examples = result.get("examples_that_can_sign_in") or []
    if examples:
        print("\nAccounts you can sign in with right now:")
        for example in examples:
            print(f"  {example['role']:9} {example['email']:28} {example['name'] or ''}")
    if result["inactive"] and not args.activate:
        print(
            f"\n{result['inactive']} account(s) are inactive or suspended and will be refused "
            "however\ncorrect the password is -- that is the offboarding rule, not a bug. Pass "
            "--activate\nwith --apply to make them all active."
        )
