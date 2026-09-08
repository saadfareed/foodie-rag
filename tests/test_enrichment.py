"""Name enrichment: resolving customer_id/vendor_id to the names a report actually shows."""

from app.agents.enrichment import enrich_rows_with_names

_USERS = [
    {"user_id": "USR-1", "name": "Ayesha Khan"},
    {"user_id": "USR-2", "name": "Bilal Aslam", "business_name": "Al-Noor Restaurant"},
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
