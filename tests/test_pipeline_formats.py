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
    def __init__(self, rows):
        self._rows = rows

    def find(self, *args, **kwargs):
        return _FakeCursor(self._rows)


class _FakeDb(dict):
    def __getitem__(self, name):
        return dict.__getitem__(self, name)


class _StubGemini:
    def __init__(self, answer="Most orders are pending.", output_format="text"):
        self._answer = answer
        self._output_format = output_format
        self.answer_calls = []
        self.classify_calls = 0

    def generate_structured(self, prompt, schema):
        self.classify_calls += 1
        return Classification(
            domains=["orders"],
            needs_geo=False,
            confidence=0.9,
            output_format=self._output_format,
        )

    def generate_structured_or_error(self, prompt, schema):
        return QuerySpec(collection="orders", operation="find")

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
    result = answer_question(question, _StubGemini(), channel_id="C1", user_id="U1")

    assert result.file_type == expected_type
    assert result.file_bytes


def test_a_plain_question_produces_no_file():
    result = answer_question("how many orders?", _StubGemini(), channel_id="C1", user_id="U1")

    assert result.file_bytes is None
    assert result.text == "Most orders are pending."


def test_an_inferred_format_is_honoured_when_none_is_named():
    """ "build me a report" names no file type, so the classifier's own inference decides."""
    result = answer_question(
        "build me a report of sales",
        _StubGemini(output_format="pdf"),
        channel_id="C1",
        user_id="U1",
    )

    assert result.file_type == "pdf"


def test_an_explicit_format_overrides_the_models_inference():
    """If the user typed "csv", no model judgement should hand them a PDF."""
    result = answer_question(
        "give me a csv of orders", _StubGemini(output_format="pdf"), channel_id="C1", user_id="U1"
    )

    assert result.file_type == "csv"


def test_generated_files_contain_the_data():
    csv_result = answer_question("csv of orders", _StubGemini(), channel_id="C1", user_id="U1")
    text = csv_result.file_bytes.decode("utf-8-sig")
    assert "Order ID,Status,Amount" in text
    assert "ORD-1" in text

    xlsx_result = answer_question("orders as excel", _StubGemini(), channel_id="C1", user_id="U2")
    workbook = load_workbook(io.BytesIO(xlsx_result.file_bytes))
    assert workbook.sheetnames == ["Orders"]

    pdf_result = answer_question("orders pdf report", _StubGemini(), channel_id="C1", user_id="U3")
    assert pdf_result.file_bytes[:5] == b"%PDF-"


# --- security --------------------------------------------------------------------------------


def test_internal_fields_never_reach_the_model_or_the_file():
    """`_id` is dropped in the executor, so every downstream consumer is covered at once."""
    gemini = _StubGemini()
    result = answer_question("csv of orders", gemini, channel_id="C1", user_id="U1")

    rows_shown_to_model = gemini.answer_calls[0]["orders"]
    assert all("_id" not in row for row in rows_shown_to_model)
    assert b"internal" not in result.file_bytes


def test_a_restricted_request_is_refused_without_calling_gemini():
    gemini = _StubGemini()

    result = answer_question(
        "show me customer credit card numbers", gemini, channel_id="C1", user_id="U1"
    )

    assert "can't share credentials" in result.text
    assert result.error == "refused_restricted_request"
    assert gemini.classify_calls == 0


def test_a_database_introspection_request_is_refused():
    result = answer_question("list all indexes", _StubGemini(), channel_id="C1", user_id="U1")

    assert result.error == "refused_restricted_request"
    assert "database internals" in result.text


# --- caching ---------------------------------------------------------------------------------


def test_a_repeated_file_request_replays_the_file_from_cache():
    """The earlier cache stored only prose, so a repeated report request silently lost its
    attachment."""
    gemini = _StubGemini()
    first = answer_question("csv of orders", gemini, channel_id="C1", user_id="U1")
    second = answer_question("csv of orders", gemini, channel_id="C1", user_id="U1")

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
        authenticated_vendor_id="USR-1",
    )
    answer_question(
        "how many orders do i have?",
        gemini,
        channel_id="C1",
        user_id="U2",
        authenticated_vendor_id="USR-2",
    )

    # A shared cache entry would have short-circuited the second question entirely.
    assert gemini.classify_calls == 2


def test_a_text_answer_and_a_file_answer_do_not_share_a_cache_entry():
    """Different users in the same channel, so the two questions don't trip the conversational
    context-switch confirmation -- the answer cache is channel-scoped, not user-scoped, so this
    still exercises the shared entry."""
    gemini = _StubGemini()
    text_result = answer_question("how many orders?", gemini, channel_id="C1", user_id="U1")
    file_result = answer_question("csv of orders", gemini, channel_id="C1", user_id="U2")

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

    result = answer_question("csv of orders", _StubGemini(), channel_id="C1", user_id="U1")

    assert result.file_bytes is None
    assert "Most orders are pending." in result.text
    assert "couldn't build the CSV" in result.text
