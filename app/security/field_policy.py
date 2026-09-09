"""The single, code-owned policy for which document fields may leave this system.

Two distinct actions, deliberately kept separate because they answer different questions:

* **DROP** -- the field is storage plumbing, not an answer. Mongo's `_id`, a Mongoose `__v`, a
  `_class` discriminator, anything an index/shard/versioning layer put there. A user asking
  "how many orders last week?" is never helped by an ObjectId, and echoing one back leaks the
  shape of the datastore (and a directly-addressable primary key) for no benefit. Dropped
  fields are removed entirely -- key and value.
* **REDACT** -- the field is real business data whose *value* is a secret or a credential:
  card numbers, CVVs, passwords, tokens, API keys, SSNs, OTPs. The column may legitimately
  appear in a report ("does this vendor have a card on file?"), so the key survives and the
  value becomes a fixed placeholder.

This module is the *only* place those rules live. `app/db/executor.py` applies it at the single
choke point where rows enter the application, so every downstream consumer -- the LLM answer
prompt, CSV/XLSX/PDF exports, the answer cache, the audit log -- receives already-sanitized
rows and cannot reintroduce raw ones. `app/rag/schema_context.py` applies the same predicate to
the schema shown to the model, so a domain agent is never even told `_id` exists and has no
name to project. `app/rag/validator.py` rejects a spec that names a denied field anyway, which
catches a model that guessed one.

That layering is the point: sanitizing at the exit (as a post-processing pass over a finished
answer) is a filter that can be forgotten. Sanitizing at the entrance means every path is
covered by construction, including paths added later.
"""

import re
from typing import Any

from app.config import settings

REDACTION_PLACEHOLDER = "[REDACTED]"

# Storage/index plumbing -- dropped outright. Matched against the *whole* field name,
# case-insensitively.
_INTERNAL_FIELD_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^_.*$"),  # _id, __v, _class, _index -- anything leading-underscore
    re.compile(r"^id$"),  # a bare `id` alias for the primary key
    re.compile(r"^(?:obj|object)_?id$"),
    re.compile(r"^.*_?(?:idx|index)$"),  # search_index, shard_idx
    re.compile(r"^(?:ns|shard|version|rev|etag|checksum|hash)$"),
)

# Real fields whose value is a credential -- key kept, value replaced.
_SECRET_FIELD_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Card patterns require a qualifier. A bare `card` is NOT treated as a secret, because it is
    # commonly a payment-*method* label rather than a number -- `payby: {"cash": 100}` /
    # `payby: {"card": 60}` breaks an order's amount down by method, and redacting that turns a
    # legitimate payment breakdown into "card=[REDACTED]". The qualified forms below are the
    # ones that actually carry a PAN.
    re.compile(r"(?:credit|debit)[_\s-]?card"),
    re.compile(r"card[_\s-]?(?:number|no|num|holder|details?|info)"),
    re.compile(r"^cardnumber$"),
    re.compile(r"^cc(?:_|$)"),  # cc, cc_num -- but NOT "account" or "occurred"
    re.compile(r"cvv|cvc"),
    re.compile(r"passw(?:or)?d|passcode"),
    re.compile(r"secret|token|api_?key|apikey"),
    re.compile(r"ssn|social_security"),
    re.compile(r"^pin$|_pin$|^otp$|_otp$"),
    re.compile(r"credential|private_key"),
    re.compile(r"^auth(?:_|$)|_auth$"),
    re.compile(r"iban|swift|routing_?number|account_?number"),
)

# Contact details -- dropped outright, not redacted.
#
# These arrived with OTP sign-in: `users.email` / `users.phone` exist so a person can prove who
# they are, and that is the only thing they are for. A model that can see them can be asked for
# them ("list my customers with their phone numbers"), which turns an authentication field into a
# contact-scraping endpoint with natural-language search over it.
#
# Dropped rather than redacted because, unlike a card number, the *column* is not useful either:
# nobody needs a report with an `email` column full of [REDACTED]. Note this also means a vendor
# cannot get their own customers' contact details through the bot even though they may legitimately
# have them elsewhere -- making that possible needs a role-aware field policy, which this
# deliberately is not (app/db/executor.py applies this at the choke point, where no role is in
# scope). That is the right trade until someone actually asks for it.
# Deliberately broader than the secret patterns above, and in the opposite direction. A false
# positive here drops one column from a report; a false negative hands over a contact list. So
# `emailed_at` being dropped along with `email` is an accepted cost -- these patterns match a
# substring, unlike the card ones, which require a qualifier precisely because `card` alone is
# usually a payment method rather than a number.
_CONTACT_FIELD_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"e-?mail"),
    re.compile(r"phone|msisdn|whatsapp"),
    re.compile(r"^mobile(?:_|$)|_mobile$"),
    re.compile(r"^contact(?:_|$)"),
)


def _matches_any(patterns: tuple[re.Pattern[str], ...], field: str) -> bool:
    lowered = field.lower()
    return any(p.search(lowered) for p in patterns)


def is_internal_field(field: str) -> bool:
    """True if `field` should never reach a user or the model at all -- storage plumbing, or a
    contact detail that exists only so someone can sign in."""
    if _matches_any(_INTERNAL_FIELD_PATTERNS, field):
        return True
    if _matches_any(_CONTACT_FIELD_PATTERNS, field):
        return True
    return field.lower() in {f.lower() for f in settings.security_extra_denied_fields}


def is_secret_field(field: str) -> bool:
    """True if `field`'s *value* is a credential that must be redacted (the key may stay)."""
    return _matches_any(_SECRET_FIELD_PATTERNS, field)


def sanitize_value(value: Any) -> Any:
    """Recursively sanitize a value, applying the field policy to any nested document keys.

    Nested subdocuments are why this isn't a flat dict comprehension: an `_id` sitting inside
    `order.payment._id` is exactly as much of a leak as a top-level one, and a `$lookup` result
    is a list of full subdocuments.
    """
    if isinstance(value, dict):
        return sanitize_document(value)
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    return value


def sanitize_document(doc: dict[str, Any]) -> dict[str, Any]:
    """Drop internal fields, redact secret ones, recurse into nested documents/arrays."""
    cleaned: dict[str, Any] = {}
    for key, value in doc.items():
        if is_internal_field(key):
            continue
        if is_secret_field(key):
            cleaned[key] = REDACTION_PLACEHOLDER
            continue
        cleaned[key] = sanitize_value(value)
    return cleaned


def sanitize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sanitize a result set. Returns new dicts -- never mutates the caller's rows, so a cached
    or reused row object can't be silently altered under another call site."""
    return [sanitize_document(row) if isinstance(row, dict) else row for row in rows]
