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
for where they land in the column order.
"""

from pymongo.database import Database

from app.security.field_policy import sanitize_rows

# id column -> the name column it resolves to.
_ID_TO_NAME_COLUMN = {"customer_id": "customer_name", "vendor_id": "vendor_name"}

# Only these are read back. A narrow projection keeps the enrichment query cheap and means the
# join can't quietly widen into "fetch every user field" as the schema grows.
_USER_PROJECTION = {"_id": 0, "user_id": 1, "name": 1, "business_name": 1}


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


def enrich_rows_with_names(db: Database, rows: list[dict], timeout_ms: int = 5000) -> list[dict]:
    """Return `rows` with `customer_name`/`vendor_name` filled in wherever an id is present.

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
    names: dict[str, str] = {}
    for user in sanitize_rows(list(users)):
        user_id = user.get("user_id")
        display = _display_name(user)
        if user_id and display:
            names[user_id] = display

    if not names:
        return rows

    enriched = []
    for row in rows:
        if not isinstance(row, dict):
            enriched.append(row)
            continue
        updated = dict(row)
        for id_column, name_column in _ID_TO_NAME_COLUMN.items():
            user_id = row.get(id_column)
            if isinstance(user_id, str) and user_id in names:
                updated[name_column] = names[user_id]
                # The id is dropped only once its name is actually in hand, so a row whose
                # lookup missed still shows the id rather than nothing. Keeping both would put
                # "USR-00031" next to "Ayesha Khan" in every report -- the same entity twice,
                # one of them in a form no reader wants.
                updated.pop(id_column, None)
        enriched.append(updated)
    return enriched
