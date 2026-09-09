"""Resolve the person/business names behind the id columns on a result row.

An order row references its customer and vendor by id (`customer_id`, `vendor_id`), because that
is how the data is actually stored. A report that shows `USR-00031` in a "Customer" column is
technically accurate and practically useless -- nobody reads a report to find out an id.

This resolves those ids to names **in code**, with one extra `users` query per question, rather
than asking the model to author a `$lookup`. Three reasons that's the right trade:

* A `$lookup` written by the model would need its own `usertype` scoping to avoid joining a
  customer row onto a vendor column, and that scoping is exactly what `scope_spec_to_domain`
  exists to keep out of the prompt's hands.
* The join is the same every time. There is nothing for a model to decide, so a model call (and
  its quota unit, and its failure modes) buys nothing.
* One `$in` query over an indexed `user_id` is cheaper than a per-row lookup and bounded by the
  result limit that already applies.

The added columns are plain data (`customer_name`, `vendor_name`), so they flow through the
report layer like any other column -- see `app/agents/domains.py::DomainConfig.report_columns`
for where they land in the column order, and `enriched_columns` for the declaration the domain
agent's prompt reads so it knows they exist.

**Names always; the rest only when asked.** "Orders with customer details" is a real request and
used to be refused by both halves at once -- the orders agent because user fields aren't in its
schema, the customers agent because order status isn't in theirs. It is answered here, because
this join is the only place that legitimately spans the two. But attaching city, loyalty tier,
category and rating to *every* order row would widen every export with columns nobody asked for,
so the extras are conditional on the question naming them (`wants_related_details`) -- a small
explicit vocabulary, for the same reason chart measures use one: this runs on the report path,
and a model call to decide it would cost a quota unit to answer something a regex knows.
"""

import re

from pymongo.database import Database

from app.security.field_policy import sanitize_rows

# id column -> the name column it resolves to.
_ID_TO_NAME_COLUMN = {"customer_id": "customer_name", "vendor_id": "vendor_name"}

#: What else a row may carry about the person behind an id: `{id column: {user field: column}}`.
#:
#: Deliberately a fixed list rather than "whatever the user document has". These are the fields
#: that describe a *party to the order*, and every one of them is already answerable through its
#: own domain -- so this widens presentation, not access. `email`/`phone` are absent and must stay
#: absent: `app/security/field_policy.py` drops them from every row, and a report may only carry
#: them through the separate, role-checked path in `app/db/identity.py::fetch_contacts`.
_RELATED_ATTRIBUTES: dict[str, dict[str, str]] = {
    "customer_id": {"city": "customer_city", "loyalty_tier": "customer_loyalty_tier"},
    "vendor_id": {"city": "vendor_city", "category": "vendor_category", "rating": "vendor_rating"},
}

# Only these are read back. A narrow projection keeps the enrichment query cheap and means the
# join can't quietly widen into "fetch every user field" as the schema grows.
_USER_PROJECTION = {
    "_id": 0,
    "user_id": 1,
    "name": 1,
    "business_name": 1,
    "city": 1,
    "loyalty_tier": 1,
    "category": 1,
    "rating": 1,
}

#: A question asking about the *people* on an order, not about the order itself. The distinction
#: this has to get right is "order details" (the order's own columns -- must NOT match) versus
#: "customer details" (the person's). Hence a required person word directly qualifying the
#: detail word, in either order, rather than a search for "details".
_PERSON_WORDS = (
    r"(?:users?|user's|customers?|customer's|clients?|buyers?"
    r"|vendors?|vendor's|sellers?|merchants?|shops?|business)"
)
_DETAIL_WORDS = r"(?:details?|info|information|profiles?|records?|data)"
_WANTS_RELATED_DETAILS = re.compile(
    rf"\b{_PERSON_WORDS}\s+{_DETAIL_WORDS}\b"
    rf"|\b{_DETAIL_WORDS}\s+(?:of|for|about)\s+"
    rf"(?:the\s+|each\s+|every\s+)?{_PERSON_WORDS}\b",
    re.IGNORECASE,
)


def wants_related_details(question: str | None) -> bool:
    """Whether the question asks for details *about the people*, not about the rows.

    "order details" is a question about orders and must not match; "customer details", "user's
    details", "details of the vendor" all must. Getting that backwards would add five columns to
    every report that says "details", which is most of them.
    """
    return bool(question) and bool(_WANTS_RELATED_DETAILS.search(question))


def _display_name(user: dict) -> str | None:
    """What to show for this user.

    `business_name` wins where present: a vendor column should read "Al-Noor Restaurant", not the
    owner's personal name. Customers have no business_name, so they fall through to `name`.
    """
    return user.get("business_name") or user.get("name")


def _collect_ids(rows: list[dict]) -> set[str]:
    ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        for id_column in _ID_TO_NAME_COLUMN:
            value = row.get(id_column)
            if isinstance(value, str) and value:
                ids.add(value)
    return ids


def enrich_rows_with_names(
    db: Database, rows: list[dict], timeout_ms: int = 5000, question: str | None = None
) -> list[dict]:
    """Return `rows` with `customer_name`/`vendor_name` filled in wherever an id is present.

    When `question` asks about the people rather than the rows (`wants_related_details`), each row
    also gains that party's city, loyalty tier, category and rating -- the "orders with customer
    details" case. One query either way: the extra fields ride along on the same `$in`.

    A no-op (no query at all) when the rows carry no ids -- an aggregation that grouped by status
    has nothing to resolve, and shouldn't pay for a lookup.

    Rows are copied rather than mutated: the caller's list may be a cached spec's result reused
    across call sites, and enrichment is presentation, not a fact about the query.
    """
    ids = _collect_ids(rows)
    if not ids:
        return rows

    users = (
        db["users"]
        .find({"user_id": {"$in": sorted(ids)}}, _USER_PROJECTION)
        .max_time_ms(timeout_ms)
    )
    # Sanitized like any other row leaving Mongo: this is a second read path out of `users`, and
    # exempting it because "it's only enrichment" is how a denied field reaches a report.
    by_id = {u["user_id"]: u for u in sanitize_rows(list(users)) if u.get("user_id")}
    if not by_id:
        return rows

    with_details = wants_related_details(question)
    enriched = []
    for row in rows:
        if not isinstance(row, dict):
            enriched.append(row)
            continue
        updated = dict(row)
        for id_column, name_column in _ID_TO_NAME_COLUMN.items():
            user_id = row.get(id_column)
            user = by_id.get(user_id) if isinstance(user_id, str) else None
            if user is None:
                continue
            display = _display_name(user)
            if display:
                updated[name_column] = display
                # The id is dropped only once its name is actually in hand, so a row whose
                # lookup missed still shows the id rather than nothing. Keeping both would put
                # "USR-00031" next to "Ayesha Khan" in every report -- the same entity twice,
                # one of them in a form no reader wants.
                updated.pop(id_column, None)
            if with_details:
                for field, column in _RELATED_ATTRIBUTES.get(id_column, {}).items():
                    value = user.get(field)
                    # Absent rather than null, matching how the rest of this data models an
                    # optional field -- a customer has no category, and an empty column headed
                    # "Vendor Category" is worse than no column at all.
                    if value is not None:
                        updated[column] = value
        enriched.append(updated)
    return _drop_constant_attributes(enriched) if with_details else enriched


def _drop_constant_attributes(rows: list[dict]) -> list[dict]:
    """Remove attribute columns whose value is the same on every row.

    A vendor asking about their own orders is answered from rows that are all theirs, so
    `vendor_city`/`vendor_category`/`vendor_rating` repeat one value down the page -- three
    columns of table width spent restating the filter. Worse than merely redundant: reports are
    capped at `REPORT_MAX_COLUMNS`, so those three pushed `created_at` off a real PDF entirely.
    The same rows fetched by an admin span many vendors, those columns vary, and they stay.

    Names are exempt and always kept: they are the point of the join, and a "Customer" column
    disappearing because one person placed every order would be a strange report. Only the
    attributes added by `_RELATED_ATTRIBUTES` are subject to this.

    Needs at least two rows to mean anything -- on a single row every column is constant, and
    dropping them all would answer "details of this order's customer" with no details.
    """
    if len(rows) < 2:
        return rows

    attribute_columns = {
        column for group in _RELATED_ATTRIBUTES.values() for column in group.values()
    }
    constant = set()
    for column in attribute_columns:
        values = {repr(row.get(column)) for row in rows if isinstance(row, dict)}
        if len(values) == 1 and any(column in row for row in rows if isinstance(row, dict)):
            constant.add(column)
    if not constant:
        return rows
    return [
        ({k: v for k, v in row.items() if k not in constant} if isinstance(row, dict) else row)
        for row in rows
    ]
