import logging

from app.rag.pipeline import answer_question
from app.rag.query_spec import QueryError, QuerySpec


class _StubGemini:
    def __init__(self, query_result, answer="the answer"):
        self._query_result = query_result
        self._answer = answer
        self.answer_calls = []

    def generate_query_spec(self, question, schema_context):
        return self._query_result

    def generate_answer(self, question, rows):
        self.answer_calls.append(rows)
        return self._answer


def test_query_error_short_circuits_to_error_message():
    gemini = _StubGemini(QueryError(error="I don't have data for that"))
    assert answer_question("what's the weather?", gemini) == "I don't have data for that"


def test_disallowed_collection_is_rejected_before_hitting_db(monkeypatch):
    monkeypatch.setattr("app.rag.pipeline.settings.mongodb_allowed_collections", ["orders"])
    gemini = _StubGemini(QuerySpec(collection="secrets", operation="find"))
    result = answer_question("show me secrets", gemini)
    assert "can't run that query" in result


def test_successful_query_calls_generate_answer_with_rows(monkeypatch):
    monkeypatch.setattr("app.rag.pipeline.settings.mongodb_allowed_collections", ["orders"])

    class _FakeCursor(list):
        def limit(self, n):
            return self

        def max_time_ms(self, n):
            return self

    class _FakeCollection:
        def find(self, *args, **kwargs):
            return _FakeCursor([{"amount": 10}, {"amount": 20}])

    class _FakeDb(dict):
        def __getitem__(self, name):
            return dict.__getitem__(self, name)

    fake_db = _FakeDb(orders=_FakeCollection())
    monkeypatch.setattr("app.rag.pipeline.get_db", lambda: fake_db)

    gemini = _StubGemini(
        QuerySpec(collection="orders", operation="find"), answer="there are 2 orders"
    )

    result = answer_question("how many orders?", gemini)

    assert result == "there are 2 orders"
    assert gemini.answer_calls == [[{"amount": 10}, {"amount": 20}]]


def test_empty_results_short_circuit_without_calling_generate_answer(monkeypatch):
    monkeypatch.setattr("app.rag.pipeline.settings.mongodb_allowed_collections", ["orders"])

    class _FakeCursor(list):
        def limit(self, n):
            return self

        def max_time_ms(self, n):
            return self

    class _FakeCollection:
        def find(self, *args, **kwargs):
            return _FakeCursor([])

    class _FakeDb(dict):
        def __getitem__(self, name):
            return dict.__getitem__(self, name)

    fake_db = _FakeDb(orders=_FakeCollection())
    monkeypatch.setattr("app.rag.pipeline.get_db", lambda: fake_db)

    gemini = _StubGemini(QuerySpec(collection="orders", operation="find"))

    result = answer_question("orders from Mars?", gemini)

    assert "didn't find any data" in result
    assert gemini.answer_calls == []


def test_successful_query_logs_per_stage_timings(monkeypatch, caplog):
    monkeypatch.setattr("app.rag.pipeline.settings.mongodb_allowed_collections", ["orders"])

    class _FakeCursor(list):
        def limit(self, n):
            return self

        def max_time_ms(self, n):
            return self

    class _FakeCollection:
        def find(self, *args, **kwargs):
            return _FakeCursor([{"amount": 10}])

    class _FakeDb(dict):
        def __getitem__(self, name):
            return dict.__getitem__(self, name)

    fake_db = _FakeDb(orders=_FakeCollection())
    monkeypatch.setattr("app.rag.pipeline.get_db", lambda: fake_db)

    gemini = _StubGemini(QuerySpec(collection="orders", operation="find"), answer="1 order")

    with caplog.at_level(logging.INFO, logger="audit"):
        answer_question("how many orders?", gemini)

    event = caplog.records[-1].event
    assert set(event["timings"]) == {"schema_context_ms", "query_gen_ms", "db_ms", "answer_gen_ms"}
    assert all(value >= 0 for value in event["timings"].values())


def test_query_error_still_logs_partial_timings(caplog):
    gemini = _StubGemini(QueryError(error="I don't have data for that"))

    with caplog.at_level(logging.INFO, logger="audit"):
        answer_question("what's the weather?", gemini)

    event = caplog.records[-1].event
    # Failed before reaching validation/execution -- only the stages actually run are timed.
    assert set(event["timings"]) == {"schema_context_ms", "query_gen_ms"}
