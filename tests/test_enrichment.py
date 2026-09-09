"""Enrichment: resolving customer_id/vendor_id to the person a report actually shows.

Names always; the rest of that person's columns only when the question asked about them. The
second half is what makes "incomplete orders with customer details" answerable at all -- before
it, both domain agents refused the question at once, each for the half it couldn't see.
"""

import pytest

from app.agents.enrichment import enrich_rows_with_names, wants_related_details

_USERS = [
    {"user_id": "USR-1", "name": "Ayesha Khan", "city": "Karachi", "loyalty_tier": "gold"},
    {
        "user_id": "USR-2",
        "name": "Bilal Aslam",
        "business_name": "Al-Noor Restaurant",
        "city": "Lahore",
        "category": "restaurant",
        "rating": 4.6,
    },
]


class _FakeCursor(list):
    def max_time_ms(self, n):
        return self


class _FakeUsers:
    def __init__(self, users, record=None):
        self._users = users
        self.record = record if record is not None else []

    def find(self, filter_, projection=None, **kwargs):
        self.record.append(filter_)
        wanted = set(filter_.get("user_id", {}).get("$in", []))
        return _FakeCursor([u for u in self._users if u["user_id"] in wanted])


class _FakeDb(dict):
    def __init__(self, users):
        super().__init__()
        self.users = _FakeUsers(users)

    def __getitem__(self, name):
        assert name == "users"
        return self.users


def test_ids_are_replaced_by_names():
    rows = [{"order_id": "ORD-1", "customer_id": "USR-1", "vendor_id": "USR-2", "amount": 10}]

    result = enrich_rows_with_names(_FakeDb(_USERS), rows)

    assert result[0]["customer_name"] == "Ayesha Khan"
    assert result[0]["vendor_name"] == "Al-Noor Restaurant"
    # The id is redundant once its name is present -- showing both puts the same entity in the
    # report twice, one of them in a form nobody reads.
    assert "customer_id" not in result[0]
    assert "vendor_id" not in result[0]
    assert result[0]["amount"] == 10


def test_business_name_wins_for_a_vendor():
    """A vendor column should read "Al-Noor Restaurant", not the owner's personal name."""
    rows = [{"vendor_id": "USR-2"}]

    assert enrich_rows_with_names(_FakeDb(_USERS), rows)[0]["vendor_name"] == "Al-Noor Restaurant"


def test_an_unresolved_id_is_left_in_place():
    """Better to show the id than to silently drop the only reference the row had."""
    rows = [{"customer_id": "USR-missing"}]

    result = enrich_rows_with_names(_FakeDb(_USERS), rows)

    assert result[0] == {"customer_id": "USR-missing"}


def test_rows_without_ids_issue_no_query():
    """An aggregation grouped by status has nothing to resolve and shouldn't pay for a lookup."""
    db = _FakeDb(_USERS)
    rows = [{"status": "pending", "count": 4}]

    assert enrich_rows_with_names(db, rows) == rows
    assert db.users.record == []


def test_one_query_covers_every_row():
    """The join is a single $in over an indexed field, not a lookup per row."""
    db = _FakeDb(_USERS)
    rows = [{"customer_id": "USR-1"} for _ in range(50)] + [{"vendor_id": "USR-2"}]

    enrich_rows_with_names(db, rows)

    assert len(db.users.record) == 1
    assert set(db.users.record[0]["user_id"]["$in"]) == {"USR-1", "USR-2"}


def test_input_rows_are_not_mutated():
    """The caller's rows may be a cached spec's result reused across call sites."""
    rows = [{"customer_id": "USR-1"}]

    enrich_rows_with_names(_FakeDb(_USERS), rows)

    assert rows == [{"customer_id": "USR-1"}]


def test_non_dict_rows_pass_through_untouched():
    """`_rows_for_prompt` can append a string marker row, and an aggregation can return scalars.
    Enrichment must step over anything that isn't a document rather than crash the question."""
    rows = [{"customer_id": "USR-1"}, "…3 more rows omitted", 42]

    result = enrich_rows_with_names(_FakeDb(_USERS), rows)

    assert result[0]["customer_name"] == "Ayesha Khan"
    assert result[1] == "…3 more rows omitted"
    assert result[2] == 42


def test_a_user_row_without_any_name_is_skipped():
    """A malformed user document shouldn't produce a blank "Customer Name" column."""
    rows = [{"customer_id": "USR-9"}]
    db = _FakeDb([{"user_id": "USR-9"}])

    assert enrich_rows_with_names(db, rows)[0] == {"customer_id": "USR-9"}


# --- the party's own columns ----------------------------------------------------------------------

_DETAILS_QUESTION = "incomplete orders with the customer details in pdf"


def test_a_question_about_the_order_gets_names_only():
    """The default, and the one that must not regress: an ordinary orders export gains a name per
    id and nothing else. Attaching five more columns to every report would be a cost paid by every
    question to serve the few that asked."""
    rows = [{"order_id": "ORD-1", "customer_id": "USR-1", "vendor_id": "USR-2"}]

    result = enrich_rows_with_names(_FakeDb(_USERS), rows, question="last 10 order details")[0]

    assert result["customer_name"] == "Ayesha Khan"
    assert "customer_city" not in result
    assert "vendor_category" not in result


def test_a_question_about_the_people_gets_their_columns_too():
    rows = [{"order_id": "ORD-1", "customer_id": "USR-1", "vendor_id": "USR-2"}]

    result = enrich_rows_with_names(_FakeDb(_USERS), rows, question=_DETAILS_QUESTION)[0]

    assert result["customer_name"] == "Ayesha Khan"
    assert result["customer_city"] == "Karachi"
    assert result["customer_loyalty_tier"] == "gold"
    assert result["vendor_name"] == "Al-Noor Restaurant"
    assert result["vendor_city"] == "Lahore"
    assert result["vendor_category"] == "restaurant"
    assert result["vendor_rating"] == 4.6


def test_the_extra_columns_cost_no_extra_query():
    """They ride along on the same `$in`. A second round trip per report to fetch fields the
    first one could have returned is the kind of thing that only shows up under load."""
    db = _FakeDb(_USERS)
    rows = [{"customer_id": "USR-1"}, {"customer_id": "USR-1"}, {"vendor_id": "USR-2"}]

    enrich_rows_with_names(db, rows, question=_DETAILS_QUESTION)

    assert len(db.users.record) == 1


def test_a_missing_attribute_is_absent_not_empty():
    """A customer has no category. An empty column headed "Vendor Category" is worse than no
    column -- it reads as missing data rather than an inapplicable field."""
    rows = [{"customer_id": "USR-1"}]

    result = enrich_rows_with_names(_FakeDb(_USERS), rows, question=_DETAILS_QUESTION)[0]

    assert "customer_city" in result
    assert "vendor_category" not in result
    assert "customer_loyalty_tier" in result


def test_contact_details_never_ride_along():
    """The one thing this join must never widen into. `email`/`phone` are dropped by the field
    policy on the way out of Mongo, and a report may only carry them through the separate,
    role-checked path in app/db/identity.py."""
    users = [{"user_id": "USR-1", "name": "A", "email": "a@b.test", "phone": "+92300"}]
    rows = [{"customer_id": "USR-1"}]

    result = enrich_rows_with_names(_FakeDb(users), rows, question=_DETAILS_QUESTION)[0]

    assert "email" not in str(result)
    assert "+92300" not in str(result)


@pytest.mark.parametrize(
    "question",
    [
        "incomplete orders with the customer details",
        "orders and user's details in pdf",
        "show me the details of the vendor for each order",
        "vendor information per order",
    ],
)
def test_questions_that_ask_about_the_people(question):
    assert wants_related_details(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "last 10 incomplete order details",
        "order details in csv",
        "how many orders are pending",
        "order info",
    ],
)
def test_questions_that_ask_only_about_the_orders(question):
    """ "order details" is the trap: it contains the detail word and means the order's own
    columns. Matching it would widen most reports in the system."""
    assert wants_related_details(question) is False


# --- columns that would say the same thing on every row -------------------------------------------


def test_an_attribute_constant_on_every_row_is_dropped():
    """A vendor's own orders are all theirs, so vendor_city/category/rating repeat one value down
    the page -- three columns of width restating the filter, and on a real PDF they pushed
    `created_at` past REPORT_MAX_COLUMNS and off the report entirely."""
    rows = [
        {"order_id": "ORD-1", "customer_id": "USR-1", "vendor_id": "USR-2"},
        {"order_id": "ORD-2", "customer_id": "USR-3", "vendor_id": "USR-2"},
    ]
    users = _USERS + [
        {"user_id": "USR-3", "name": "Sana Mirza", "city": "Lahore", "loyalty_tier": "bronze"}
    ]

    result = enrich_rows_with_names(_FakeDb(users), rows, question=_DETAILS_QUESTION)

    assert "vendor_city" not in result[0], "same vendor on every row"
    assert "vendor_category" not in result[0]
    assert result[0]["customer_city"] == "Karachi", "customers differ, so their columns stay"
    assert result[1]["customer_city"] == "Lahore"


def test_the_name_stays_even_when_it_is_constant():
    """It is the point of the join. A "Vendor" column vanishing because one vendor placed every
    order would read as missing data."""
    rows = [{"vendor_id": "USR-2"}, {"vendor_id": "USR-2"}]

    result = enrich_rows_with_names(_FakeDb(_USERS), rows, question=_DETAILS_QUESTION)

    assert all(row["vendor_name"] == "Al-Noor Restaurant" for row in result)


def test_a_single_row_keeps_its_attributes():
    """On one row every column is constant -- dropping them would answer "this order's customer
    details" with no details."""
    rows = [{"customer_id": "USR-1"}]

    result = enrich_rows_with_names(_FakeDb(_USERS), rows, question=_DETAILS_QUESTION)[0]

    assert result["customer_city"] == "Karachi"
    assert result["customer_loyalty_tier"] == "gold"


def test_a_varying_attribute_survives():
    rows = [{"customer_id": "USR-1"}, {"customer_id": "USR-3"}]
    users = _USERS + [
        {"user_id": "USR-3", "name": "Sana", "city": "Lahore", "loyalty_tier": "bronze"}
    ]

    result = enrich_rows_with_names(_FakeDb(users), rows, question=_DETAILS_QUESTION)

    assert [row["customer_loyalty_tier"] for row in result] == ["gold", "bronze"]
