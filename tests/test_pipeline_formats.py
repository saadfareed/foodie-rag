"""Pipeline behaviour around output formats, refusals, and the security layer.

Kept separate from tests/test_pipeline.py, which covers routing/caching/clarification, so each
file stays readable. The stubs mirror that file's `_StubGemini` / `_FakeDb` conventions rather
than introducing a mocking framework.
"""

import io

import pytest
from openpyxl import load_workbook

from app.agents.classifier import Classification
from app.rag.answer_cache import AnswerCache
from app.rag.clarification_cache import ClarificationCache
from app.rag.context_switch_cache import ContextSwitchCache
from app.rag.conversation_context import ConversationContextCache
from app.rag.pipeline import answer_question
from app.rag.query_spec import QuerySpec
from app.security.roles import Principal, Role, admin

#: These tests exercise formats, caching and bad input -- not authorization -- so they run as
#: an operator. Stated explicitly rather than inherited: answer_question defaults to ANONYMOUS,
#: which can read nothing, so a forgotten principal fails loudly instead of seeing everything.
ADMIN = admin()


def vendor(user_id: str) -> Principal:
    """A signed-in vendor -- what `/login USR-00031` produces, and what the web adapter mints for
    a vendor session."""
    return Principal(role=Role.VENDOR, user_id=user_id)


_ROWS = [
    {
        "_id": "internal",
        "order_id": f"ORD-{i}",
        "status": ["pending", "delivered"][i % 2],
        "amount": i * 10.0,
    }
    for i in range(1, 7)
]


class _FakeCursor(list):
    def limit(self, n):
        return self

    def max_time_ms(self, n):
        return self


class _FakeCollection:
    def __init__(self, rows, aggregate_rows=None):
        self._rows = rows
        self._aggregate_rows = aggregate_rows or []

    def find(self, *args, **kwargs):
        return _FakeCursor(self._rows)

    def aggregate(self, pipeline, **kwargs):
        return iter(self._aggregate_rows)


class _FakeDb(dict):
    def __getitem__(self, name):
        # A collection nobody set up is empty, not an error -- which is what a real database does,
        # and what the vendor -> customers authorization lookup hits when a test only cares about
        # `users`.
        if name not in self:
            return _FakeCollection([])
        return dict.__getitem__(self, name)


class _StubGemini:
    def __init__(self, answer="Most orders are pending.", output_format="text", domains=None):
        self._answer = answer
        self._output_format = output_format
        # Which domain the classifier claims. Defaults to orders; the contact-column tests need
        # `customers`/`vendors`, whose rows are `users` documents carrying a user_id.
        self._domains = domains or ["orders"]
        self.answer_calls = []
        self.classify_calls = 0

    def generate_structured(self, prompt, schema):
        self.classify_calls += 1
        return Classification(
            domains=self._domains,
            needs_geo=False,
            confidence=0.9,
            output_format=self._output_format,
        )

    def generate_structured_or_error(self, prompt, schema):
        collection = "orders" if self._domains == ["orders"] else "users"
        return QuerySpec(collection=collection, operation="find")

    def generate_answer(self, question, rows_by_domain, skipped_domains=None):
        self.answer_calls.append(rows_by_domain)
        return self._answer


@pytest.fixture(autouse=True)
def _isolated_caches(monkeypatch):
    monkeypatch.setattr("app.rag.pipeline.answer_cache", AnswerCache(1800, 500))
    monkeypatch.setattr("app.rag.pipeline.clarification_cache", ClarificationCache(300, 500))
    monkeypatch.setattr(
        "app.rag.pipeline.conversation_context_cache", ConversationContextCache(300, 500)
    )
    monkeypatch.setattr("app.rag.pipeline.context_switch_cache", ContextSwitchCache(120, 500))


@pytest.fixture(autouse=True)
def _fake_mongo(monkeypatch):
    monkeypatch.setattr("app.agents.graph.get_db", lambda: _FakeDb(orders=_FakeCollection(_ROWS)))


# --- format selection ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question,expected_type",
    [
        ("give me a csv of orders", "csv"),
        ("export orders to excel", "xlsx"),
        ("pdf report of orders", "pdf"),
    ],
)
def test_an_explicitly_named_format_produces_that_file(question, expected_type):
    result = answer_question(
        question, _StubGemini(), channel_id="C1", user_id="U1", principal=ADMIN
    )

    assert result.file_type == expected_type
    assert result.file_bytes


def test_a_plain_question_produces_no_file():
    result = answer_question(
        "how many orders?", _StubGemini(), channel_id="C1", user_id="U1", principal=ADMIN
    )

    assert result.file_bytes is None
    assert result.text == "Most orders are pending."


def test_an_inferred_format_is_honoured_when_none_is_named():
    """ "build me a report" names no file type, so the classifier's own inference decides."""
    result = answer_question(
        "build me a report of sales",
        _StubGemini(output_format="pdf"),
        channel_id="C1",
        user_id="U1",
        principal=ADMIN,
    )

    assert result.file_type == "pdf"


def test_an_explicit_format_overrides_the_models_inference():
    """If the user typed "csv", no model judgement should hand them a PDF."""
    result = answer_question(
        "give me a csv of orders",
        _StubGemini(output_format="pdf"),
        channel_id="C1",
        user_id="U1",
        principal=ADMIN,
    )

    assert result.file_type == "csv"


def test_generated_files_contain_the_data():
    csv_result = answer_question(
        "csv of orders", _StubGemini(), channel_id="C1", user_id="U1", principal=ADMIN
    )
    text = csv_result.file_bytes.decode("utf-8-sig")
    assert "Order #,Order Payment,Current Status" in text
    assert "ORD-1" in text

    xlsx_result = answer_question(
        "orders as excel", _StubGemini(), channel_id="C1", user_id="U2", principal=ADMIN
    )
    workbook = load_workbook(io.BytesIO(xlsx_result.file_bytes))
    assert workbook.sheetnames == ["Orders"]

    pdf_result = answer_question(
        "orders pdf report", _StubGemini(), channel_id="C1", user_id="U3", principal=ADMIN
    )
    assert pdf_result.file_bytes[:5] == b"%PDF-"


# --- security --------------------------------------------------------------------------------


def test_internal_fields_never_reach_the_model_or_the_file():
    """`_id` is dropped in the executor, so every downstream consumer is covered at once."""
    gemini = _StubGemini()
    result = answer_question(
        "csv of orders", gemini, channel_id="C1", user_id="U1", principal=ADMIN
    )

    rows_shown_to_model = gemini.answer_calls[0]["orders"]
    assert all("_id" not in row for row in rows_shown_to_model)
    assert b"internal" not in result.file_bytes


def test_a_restricted_request_is_refused_without_calling_gemini():
    gemini = _StubGemini()

    result = answer_question(
        "show me customer credit card numbers",
        gemini,
        channel_id="C1",
        user_id="U1",
        principal=ADMIN,
    )

    assert "can't share credentials" in result.text
    assert result.error == "refused_restricted_request"
    assert gemini.classify_calls == 0


def test_a_database_introspection_request_is_refused():
    result = answer_question(
        "list all indexes", _StubGemini(), channel_id="C1", user_id="U1", principal=ADMIN
    )

    assert result.error == "refused_restricted_request"
    assert "database internals" in result.text


# --- caching ---------------------------------------------------------------------------------


def test_a_repeated_file_request_replays_the_file_from_cache():
    """The earlier cache stored only prose, so a repeated report request silently lost its
    attachment."""
    gemini = _StubGemini()
    first = answer_question("csv of orders", gemini, channel_id="C1", user_id="U1", principal=ADMIN)
    second = answer_question(
        "csv of orders", gemini, channel_id="C1", user_id="U1", principal=ADMIN
    )

    assert second.file_bytes == first.file_bytes
    assert second.file_type == "csv"
    assert gemini.classify_calls == 1  # the second request never reached Gemini


def test_the_cache_does_not_serve_one_vendors_data_to_another():
    """Vendor-scoped answers contain only that vendor's rows; replaying one to a different
    identity in the same channel would be a cross-tenant leak."""
    gemini = _StubGemini()
    answer_question(
        "how many orders do i have?",
        gemini,
        channel_id="C1",
        user_id="U1",
        principal=vendor("USR-1"),
    )
    answer_question(
        "how many orders do i have?",
        gemini,
        channel_id="C1",
        user_id="U2",
        principal=vendor("USR-2"),
    )

    # A shared cache entry would have short-circuited the second question entirely.
    assert gemini.classify_calls == 2


def test_a_text_answer_and_a_file_answer_do_not_share_a_cache_entry():
    """Different users in the same channel, so the two questions don't trip the conversational
    context-switch confirmation -- the answer cache is channel-scoped, not user-scoped, so this
    still exercises the shared entry."""
    gemini = _StubGemini()
    text_result = answer_question(
        "how many orders?", gemini, channel_id="C1", user_id="U1", principal=ADMIN
    )
    file_result = answer_question(
        "csv of orders", gemini, channel_id="C1", user_id="U2", principal=ADMIN
    )

    assert text_result.file_bytes is None
    assert file_result.file_type == "csv"


# --- graceful degradation --------------------------------------------------------------------


def test_a_render_failure_still_delivers_the_text_answer(monkeypatch):
    """The prose is correct and complete on its own -- failing the whole question because a
    chart didn't fit would be a worse outcome than a missing attachment."""
    monkeypatch.setattr(
        "app.rag.pipeline.generate_csv",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    result = answer_question(
        "csv of orders", _StubGemini(), channel_id="C1", user_id="U1", principal=ADMIN
    )

    assert result.file_bytes is None
    assert "Most orders are pending." in result.text
    assert "couldn't build the CSV" in result.text


# --- report title ----------------------------------------------------------------------------


def _title_of_csv(result):
    return result.file_bytes.decode("utf-8-sig").splitlines()[0]


def test_the_report_is_titled_after_the_request():
    """A generic "Data Report" on a specific request makes the document feel like it wasn't
    actually about what was asked."""

    class _TitledGemini(_StubGemini):
        def generate_structured(self, prompt, schema):
            self.classify_calls += 1
            return Classification(
                domains=["orders"],
                confidence=0.9,
                output_format="csv",
                report_title="Last 10 Incomplete Order Details",
            )

    result = answer_question(
        "I need last 10 incomplete order details in csv",
        _TitledGemini(),
        channel_id="C1",
        user_id="U1",
        principal=ADMIN,
    )

    assert _title_of_csv(result) == "Last 10 Incomplete Order Details"


def test_the_configured_title_is_used_when_the_model_has_no_opinion(monkeypatch):
    monkeypatch.setattr("app.rag.pipeline.settings.report_title", "Data Report")

    result = answer_question(
        "csv of orders", _StubGemini(), channel_id="C1", user_id="U1", principal=ADMIN
    )

    assert _title_of_csv(result) == "Data Report"


def test_a_blank_model_title_falls_back_rather_than_titling_the_file_empty(monkeypatch):
    monkeypatch.setattr("app.rag.pipeline.settings.report_title", "Data Report")

    class _BlankTitleGemini(_StubGemini):
        def generate_structured(self, prompt, schema):
            self.classify_calls += 1
            return Classification(
                domains=["orders"], confidence=0.9, output_format="csv", report_title="   "
            )

    result = answer_question(
        "csv of orders", _BlankTitleGemini(), channel_id="C1", user_id="U1", principal=ADMIN
    )

    assert _title_of_csv(result) == "Data Report"


def test_the_pdf_carries_the_same_title(monkeypatch):
    captured = {}
    import app.generators.pdf_generator as pdf_module

    original = pdf_module._TEMPLATE.render
    monkeypatch.setattr(
        pdf_module._TEMPLATE, "render", lambda **kw: captured.update(kw) or original(**kw)
    )

    class _TitledGemini(_StubGemini):
        def generate_structured(self, prompt, schema):
            self.classify_calls += 1
            return Classification(
                domains=["orders"],
                confidence=0.9,
                output_format="pdf",
                report_title="Last 10 Incomplete Order Details",
            )

    answer_question(
        "orders report", _TitledGemini(), channel_id="C1", user_id="U1", principal=ADMIN
    )

    assert captured["title"] == "Last 10 Incomplete Order Details"


# --- report generation edge cases ------------------------------------------------------------


def test_no_file_is_built_when_the_query_returned_nothing(monkeypatch):
    """An empty CSV is worse than no CSV -- the text answer already says nothing was found."""
    monkeypatch.setattr("app.agents.graph.get_db", lambda: _FakeDb(orders=_FakeCollection([])))

    result = answer_question(
        "csv of orders", _StubGemini(), channel_id="C1", user_id="U1", principal=ADMIN
    )

    assert result.file_bytes is None


def test_an_unknown_output_format_degrades_to_text():
    """Defence in depth: the field is enum-constrained, but a format with no builder must not
    take the answer down with it."""
    result = answer_question(
        "orders", _StubGemini(output_format="text"), channel_id="C1", user_id="U1", principal=ADMIN
    )

    assert result.file_bytes is None
    assert result.text


def test_a_render_timeout_still_delivers_the_text_answer(monkeypatch):
    """A pathological table must not leave the user waiting, or empty-handed."""
    from app.generators.render_pool import RenderTimeout

    def _timeout(fn, description=""):
        raise RenderTimeout(description)

    monkeypatch.setattr("app.rag.pipeline.run_render", _timeout)

    result = answer_question(
        "csv of orders", _StubGemini(), channel_id="C1", user_id="U1", principal=ADMIN
    )

    assert result.file_bytes is None
    assert "Most orders are pending." in result.text
    assert "couldn't build the CSV" in result.text


def test_build_file_returns_nothing_for_a_text_answer():
    """Defensive: the caller already guards on this, but _build_file must not try to render a
    document for a format that has none."""
    from app.rag.pipeline import _build_file

    assert _build_file("text", {"orders": [{"a": 1}]}, "q", "a", "T") is None


def test_build_file_returns_nothing_for_a_format_with_no_builder():
    from app.rag.pipeline import _build_file

    assert _build_file("docx", {"orders": [{"a": 1}]}, "q", "a", "T") is None


# --- RBAC end to end ---------------------------------------------------------------------


def test_a_logged_in_vendors_question_is_scoped_to_their_own_rows(monkeypatch):
    """End-to-end companion to the graph-level RBAC tests: the vendor id has to survive the
    whole pipeline -> graph -> Send-payload -> query path, and it previously did not."""
    captured = {}

    class _CapturingCollection(_FakeCollection):
        def find(self, filter_=None, *args, **kwargs):
            captured["filter"] = filter_
            return _FakeCursor(self._rows)

    monkeypatch.setattr(
        "app.agents.graph.get_db", lambda: _FakeDb(orders=_CapturingCollection(_ROWS))
    )

    answer_question(
        "how many orders do i have?",
        _StubGemini(),
        channel_id="C1",
        user_id="U1",
        principal=vendor("USR-42"),
    )

    assert "USR-42" in str(captured["filter"])


def test_the_cache_does_not_serve_a_vendors_answer_to_a_customer():
    """Two principals can share a `user_id` -- the id is only unique within a usertype -- and they
    are answered from entirely different rows. A cache key carrying only the id would replay one's
    answer to the other, which is a cross-role leak wearing a familiar-looking key."""
    gemini = _StubGemini()
    answer_question(
        "how many orders do i have?",
        gemini,
        channel_id="C1",
        user_id="U1",
        principal=Principal(role=Role.VENDOR, user_id="USR-1"),
    )
    answer_question(
        "how many orders do i have?",
        gemini,
        channel_id="C1",
        user_id="U2",
        principal=Principal(role=Role.CUSTOMER, user_id="USR-1"),
    )

    assert gemini.classify_calls == 2


# --- per-role limits and contact columns in a report ---------------------------------------


def test_a_roles_report_row_cap_is_applied(monkeypatch):
    """Rendering is the most expensive CPU on the request path and runs on a bounded pool, so how
    many rows a role may ask WeasyPrint for is a real load question once customers can reach it."""
    monkeypatch.setattr("app.rag.pipeline.settings.report_max_rows_by_role", {"customer": 2})
    rows = [{"order_id": f"ORD-{i}", "amount": i} for i in range(10)]
    monkeypatch.setattr("app.agents.graph.get_db", lambda: _FakeDb(orders=_FakeCollection(rows)))

    result = answer_question(
        "give me a csv of orders",
        _StubGemini(),
        channel_id="C1",
        user_id="U1",
        principal=Principal(role=Role.CUSTOMER, user_id="USR-C1"),
    )

    body = result.file_bytes.decode("utf-8-sig")
    # Two data rows plus the header and the title block -- the point is that ten did not survive.
    assert body.count("ORD-") == 2


def test_the_role_cap_cannot_exceed_the_absolute_ceiling(monkeypatch):
    """An override may only lower the cap. Otherwise a role could be configured past the bound
    the render pool was sized for."""
    monkeypatch.setattr("app.generators.tabular.settings.report_max_rows", 3)
    monkeypatch.setattr("app.rag.pipeline.settings.report_max_rows_by_role", {"admin": 100})
    rows = [{"order_id": f"ORD-{i}"} for i in range(10)]
    monkeypatch.setattr("app.agents.graph.get_db", lambda: _FakeDb(orders=_FakeCollection(rows)))

    result = answer_question(
        "give me a csv of orders", _StubGemini(), channel_id="C1", user_id="U1", principal=ADMIN
    )

    assert result.file_bytes.decode("utf-8-sig").count("ORD-") == 3


def test_contact_columns_are_off_unless_turned_on(monkeypatch):
    """Exposing contact details is a deliberate operator decision, not something that arrives
    with an upgrade."""
    monkeypatch.setattr("app.rag.pipeline.settings.report_include_contacts", False)
    rows = [{"user_id": "USR-C1", "name": "Ayesha Khan"}]
    monkeypatch.setattr(
        "app.agents.graph.get_db",
        lambda: _FakeDb(
            users=_FakeCollection(rows),
            orders=_FakeCollection([], aggregate_rows=[{"_id": "USR-C1"}]),
        ),
    )
    monkeypatch.setattr(
        "app.rag.pipeline.fetch_contacts",
        lambda db, ids: {"USR-C1": {"email": "a@b.test", "phone": "+92300"}},
    )

    result = answer_question(
        "csv of my customers",
        _StubGemini(domains=["customers"]),
        channel_id="C1",
        user_id="U1",
        principal=vendor("USR-V1"),
    )

    assert b"a@b.test" not in (result.file_bytes or b"")


def test_a_vendor_report_can_carry_contacts_for_their_own_customers(monkeypatch):
    monkeypatch.setattr("app.rag.pipeline.settings.report_include_contacts", True)
    rows = [{"user_id": "USR-C1", "name": "Ayesha Khan"}]

    def _db():
        return _FakeDb(
            users=_FakeCollection(rows),
            # The vendor -> customers scope is resolved from this vendor's own orders.
            orders=_FakeCollection([], aggregate_rows=[{"_id": "USR-C1"}]),
        )

    monkeypatch.setattr("app.agents.graph.get_db", _db)
    monkeypatch.setattr("app.rag.pipeline.get_db", _db)
    monkeypatch.setattr(
        "app.rag.pipeline.fetch_contacts",
        lambda db, ids: {"USR-C1": {"email": "a@b.test", "phone": "+92300"}},
    )

    result = answer_question(
        "csv of my customers",
        _StubGemini(domains=["customers"]),
        channel_id="C1",
        user_id="U1",
        principal=vendor("USR-V1"),
    )

    assert b"a@b.test" in result.file_bytes


def test_a_customer_gets_no_contacts_for_the_vendor_directory(monkeypatch):
    """The exception that matters: `vendors` is unfiltered for a customer, so a contact column
    there would be every vendor's phone number in one download."""
    monkeypatch.setattr("app.rag.pipeline.settings.report_include_contacts", True)
    rows = [{"user_id": "USR-V1", "business_name": "Kifayat Foods"}]
    monkeypatch.setattr("app.agents.graph.get_db", lambda: _FakeDb(users=_FakeCollection(rows)))
    monkeypatch.setattr("app.rag.pipeline.get_db", lambda: _FakeDb(users=_FakeCollection(rows)))
    monkeypatch.setattr(
        "app.rag.pipeline.fetch_contacts",
        lambda db, ids: {"USR-V1": {"phone": "+92300"}},
    )

    result = answer_question(
        "csv of vendors",
        _StubGemini(domains=["vendors"]),
        channel_id="C1",
        user_id="U1",
        principal=Principal(role=Role.CUSTOMER, user_id="USR-C1"),
    )

    assert b"+92300" not in (result.file_bytes or b"")


def test_a_daily_limit_stops_a_slow_drain(monkeypatch):
    """Per-minute limits never notice one question every thirty seconds, and that still exhausts
    a free-tier quota by lunchtime."""
    monkeypatch.setattr("app.rag.pipeline.daily_question_limiter._daily_limit", 2)
    monkeypatch.setattr("app.agents.graph.get_db", lambda: _FakeDb(orders=_FakeCollection(_ROWS)))
    gemini = _StubGemini()

    texts = [
        answer_question(
            f"question {i}", gemini, channel_id="C1", user_id="U1", principal=ADMIN
        ).text
        for i in range(3)
    ]

    assert "limit for today" in texts[-1]


def test_the_daily_limit_follows_the_person_not_the_channel(monkeypatch):
    monkeypatch.setattr("app.rag.pipeline.daily_question_limiter._daily_limit", 1)
    monkeypatch.setattr("app.agents.graph.get_db", lambda: _FakeDb(orders=_FakeCollection(_ROWS)))
    gemini = _StubGemini()

    answer_question("q", gemini, channel_id="C1", user_id="U1", principal=vendor("USR-1"))
    same_person_elsewhere = answer_question(
        "q", gemini, channel_id="C2", user_id="U1", principal=vendor("USR-1")
    )
    someone_else = answer_question(
        "q", gemini, channel_id="C1", user_id="U2", principal=vendor("USR-2")
    )

    assert "limit for today" in same_person_elsewhere.text
    assert "limit for today" not in someone_else.text
