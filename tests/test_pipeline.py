import logging

from app.rag.answer_cache import AnswerCache
from app.rag.pipeline import answer_question
from app.rag.query_spec import QueryError, QuerySpec


class _StubGemini:
    def __init__(self, query_result, answer="the answer"):
        self._query_result = query_result
        self._answer = answer
        self.answer_calls = []
        self.query_spec_calls = []

    def generate_query_spec(self, question, schema_context):
        self.query_spec_calls.append(question)
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


def test_query_generation_failure_still_records_its_own_stage_timing(caplog):
    """Regression test for a production bug: the query-generation stage's own duration must be
    captured in the log even when that stage is the one that raised -- previously, log_query_event
    was called (and the log line serialized) from inside the except block *before* the timing
    context manager's finally had a chance to record query_gen_ms, so a request that failed during
    query generation showed only schema_context_ms in `timings`, hiding exactly the stage that was
    slow/failing (e.g. a Gemini HTTP timeout)."""

    class _SlowFailingGemini:
        def generate_query_spec(self, question, schema_context):
            raise TimeoutError("The read operation timed out")

    with caplog.at_level(logging.INFO, logger="audit"):
        result = answer_question("how many cash orders?", _SlowFailingGemini())

    assert "couldn't process that question" in result
    event = caplog.records[-1].event
    assert "query_gen_ms" in event["timings"]
    assert event["timings"]["query_gen_ms"] >= 0


def test_answer_generation_failure_is_handled_gracefully(monkeypatch, caplog):
    """generate_answer used to have no error handling at all -- a timeout there would propagate
    unhandled out of answer_question instead of returning a graceful message like the
    generate_query_spec path already does."""
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

    monkeypatch.setattr("app.rag.pipeline.get_db", lambda: _FakeDb(orders=_FakeCollection()))

    class _FailsOnAnswerGemini:
        def generate_query_spec(self, question, schema_context):
            return QuerySpec(collection="orders", operation="find")

        def generate_answer(self, question, rows):
            raise TimeoutError("The read operation timed out")

    with caplog.at_level(logging.INFO, logger="audit"):
        result = answer_question("how many orders?", _FailsOnAnswerGemini())

    assert "couldn't put it into words" in result
    event = caplog.records[-1].event
    assert "answer_gen_ms" in event["timings"]


def _fake_db_with_one_order_row():
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

    return _FakeDb(orders=_FakeCollection())


def test_repeated_question_in_same_channel_is_served_from_cache(monkeypatch):
    monkeypatch.setattr("app.rag.pipeline.settings.mongodb_allowed_collections", ["orders"])
    monkeypatch.setattr(
        "app.rag.pipeline.answer_cache", AnswerCache(ttl_seconds=60, max_entries=10)
    )
    monkeypatch.setattr("app.rag.pipeline.get_db", lambda: _fake_db_with_one_order_row())

    gemini = _StubGemini(QuerySpec(collection="orders", operation="find"), answer="cached answer")

    first = answer_question("How many orders?", gemini, channel_id="C1")
    second = answer_question("  how many orders?  ", gemini, channel_id="C1")

    assert first == second == "cached answer"
    assert len(gemini.query_spec_calls) == 1
    assert len(gemini.answer_calls) == 1


def test_cache_is_scoped_per_channel(monkeypatch):
    monkeypatch.setattr("app.rag.pipeline.settings.mongodb_allowed_collections", ["orders"])
    monkeypatch.setattr(
        "app.rag.pipeline.answer_cache", AnswerCache(ttl_seconds=60, max_entries=10)
    )
    monkeypatch.setattr("app.rag.pipeline.get_db", lambda: _fake_db_with_one_order_row())

    gemini = _StubGemini(QuerySpec(collection="orders", operation="find"), answer="answer")

    answer_question("how many orders?", gemini, channel_id="C1")
    answer_question("how many orders?", gemini, channel_id="C2")

    assert len(gemini.query_spec_calls) == 2


def test_cache_hit_logs_cache_hit_true(monkeypatch, caplog):
    monkeypatch.setattr("app.rag.pipeline.settings.mongodb_allowed_collections", ["orders"])
    monkeypatch.setattr(
        "app.rag.pipeline.answer_cache", AnswerCache(ttl_seconds=60, max_entries=10)
    )
    monkeypatch.setattr("app.rag.pipeline.get_db", lambda: _fake_db_with_one_order_row())

    gemini = _StubGemini(QuerySpec(collection="orders", operation="find"), answer="answer")

    with caplog.at_level(logging.INFO, logger="audit"):
        answer_question("how many orders?", gemini, channel_id="C1")
        first_event = caplog.records[-1].event
        answer_question("how many orders?", gemini, channel_id="C1")
        second_event = caplog.records[-1].event

    assert first_event["cache_hit"] is False
    assert second_event["cache_hit"] is True


def test_quota_exceeded_responses_are_not_cached(monkeypatch):
    monkeypatch.setattr(
        "app.rag.pipeline.answer_cache", AnswerCache(ttl_seconds=60, max_entries=10)
    )
    monkeypatch.setattr("app.rag.pipeline.quota_tracker.is_over_budget", lambda: True)
    gemini = _StubGemini(QuerySpec(collection="orders", operation="find"))

    answer_question("how many orders?", gemini, channel_id="C1")
    answer_question("how many orders?", gemini, channel_id="C1")

    assert gemini.query_spec_calls == []


def test_query_error_responses_are_cached(monkeypatch):
    monkeypatch.setattr(
        "app.rag.pipeline.answer_cache", AnswerCache(ttl_seconds=60, max_entries=10)
    )
    gemini = _StubGemini(QueryError(error="I don't have data for that"))

    first = answer_question("what's the weather?", gemini, channel_id="C1")
    second = answer_question("what's the weather?", gemini, channel_id="C1")

    assert first == second == "I don't have data for that"
    assert len(gemini.query_spec_calls) == 1


def test_no_rows_found_responses_are_cached(monkeypatch):
    monkeypatch.setattr("app.rag.pipeline.settings.mongodb_allowed_collections", ["orders"])
    monkeypatch.setattr(
        "app.rag.pipeline.answer_cache", AnswerCache(ttl_seconds=60, max_entries=10)
    )

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

    monkeypatch.setattr("app.rag.pipeline.get_db", lambda: _FakeDb(orders=_FakeCollection()))
    gemini = _StubGemini(QuerySpec(collection="orders", operation="find"))

    answer_question("orders from Mars?", gemini, channel_id="C1")
    answer_question("orders from Mars?", gemini, channel_id="C1")

    assert len(gemini.query_spec_calls) == 1


def test_gemini_exceptions_are_not_cached(monkeypatch):
    monkeypatch.setattr(
        "app.rag.pipeline.answer_cache", AnswerCache(ttl_seconds=60, max_entries=10)
    )

    class _AlwaysFailingGemini:
        def __init__(self):
            self.calls = 0

        def generate_query_spec(self, question, schema_context):
            self.calls += 1
            raise TimeoutError("The read operation timed out")

    gemini = _AlwaysFailingGemini()

    answer_question("how many cash orders?", gemini, channel_id="C1")
    answer_question("how many cash orders?", gemini, channel_id="C1")

    assert gemini.calls == 2
