import logging

import pytest

from app.agents.classifier import Classification
from app.rag.answer_cache import AnswerCache
from app.rag.clarification_cache import ClarificationCache, PendingClarification
from app.rag.context_switch_cache import ContextSwitchCache, PendingContextSwitch
from app.rag.conversation_context import ConversationContextCache
from app.rag.pipeline import answer_question
from app.rag.query_spec import QueryError, QuerySpec
from app.rag.rate_limiter import rate_limiter


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


def _fake_db(orders_rows):
    return _FakeDb(orders=_FakeCollection(orders_rows))


class _StubGemini:
    """Classifies everything into `orders`, generates whatever QuerySpec/QueryError/answer
    it's constructed with -- mirrors the shape app/agents/graph.py actually calls."""

    def __init__(
        self,
        query_result=None,
        answer="the answer",
        domains=("orders",),
        confidence=0.9,
        clarification_question=None,
    ):
        self._query_result = query_result or QuerySpec(collection="orders", operation="find")
        self._answer = answer
        self._domains = list(domains)
        self._confidence = confidence
        self._clarification_question = clarification_question
        self.answer_calls = []
        self.query_spec_calls = 0

    def generate_structured(self, prompt, schema):
        # resolved_question intentionally left unset here -- classify_question() backfills it to
        # the question that was actually classified, mirroring what a real Gemini response
        # omitting the field would get.
        return Classification(
            domains=self._domains,
            needs_geo=False,
            confidence=self._confidence,
            clarification_question=self._clarification_question,
        )

    def generate_structured_or_error(self, prompt, schema):
        self.query_spec_calls += 1
        return self._query_result

    def generate_answer(self, question, rows_by_domain, skipped_domains=None):
        self.answer_calls.append(rows_by_domain)
        return self._answer


def _patch_shared_caches(monkeypatch, ttl_seconds=1800, max_entries=500):
    monkeypatch.setattr(
        "app.rag.pipeline.answer_cache",
        AnswerCache(ttl_seconds=ttl_seconds, max_entries=max_entries),
    )
    monkeypatch.setattr(
        "app.rag.pipeline.clarification_cache",
        ClarificationCache(ttl_seconds=300, max_entries=500),
    )
    monkeypatch.setattr(
        "app.rag.pipeline.conversation_context_cache",
        ConversationContextCache(ttl_seconds=300, max_entries=500),
    )
    monkeypatch.setattr(
        "app.rag.pipeline.context_switch_cache",
        ContextSwitchCache(ttl_seconds=120, max_entries=500),
    )


def test_query_error_short_circuits_to_error_message(monkeypatch):
    _patch_shared_caches(monkeypatch)
    gemini = _StubGemini(query_result=QueryError(error="I don't have data for that"))
    assert answer_question("what's the weather?", gemini).text == "I don't have data for that"


def test_hallucinated_collection_name_is_overridden_not_honored(monkeypatch):
    """scope_spec_to_domain (app/agents/domains.py) forces the spec's collection to whatever the
    classified domain is actually configured for, regardless of what the model wrote -- so a
    model that hallucinates collection="secrets" for an "orders" question still only ever
    touches 'orders'."""
    _patch_shared_caches(monkeypatch)
    queried_collections = []

    class _TrackingCollection(_FakeCollection):
        def find(self, *args, **kwargs):
            queried_collections.append("orders")
            return super().find(*args, **kwargs)

    fake_db = _FakeDb(orders=_TrackingCollection([{"amount": 10}]))
    monkeypatch.setattr("app.agents.graph.get_db", lambda: fake_db)

    gemini = _StubGemini(query_result=QuerySpec(collection="secrets", operation="find"))
    answer_question("show me orders", gemini)

    assert queried_collections == ["orders"]


def test_successful_query_calls_generate_answer_with_rows(monkeypatch):
    _patch_shared_caches(monkeypatch)
    monkeypatch.setattr(
        "app.agents.graph.get_db", lambda: _fake_db([{"amount": 10}, {"amount": 20}])
    )

    gemini = _StubGemini(
        query_result=QuerySpec(collection="orders", operation="find"), answer="there are 2 orders"
    )

    result = answer_question("how many orders?", gemini)

    assert result.text == "there are 2 orders"
    assert gemini.answer_calls == [{"orders": [{"amount": 10}, {"amount": 20}]}]


def test_empty_results_short_circuit_without_calling_generate_answer(monkeypatch):
    _patch_shared_caches(monkeypatch)
    monkeypatch.setattr("app.agents.graph.get_db", lambda: _fake_db([]))

    gemini = _StubGemini(query_result=QuerySpec(collection="orders", operation="find"))

    result = answer_question("orders from Mars?", gemini)

    assert "nothing matched that" in result.text
    assert gemini.answer_calls == []


def test_successful_query_logs_per_stage_timings(monkeypatch, caplog):
    _patch_shared_caches(monkeypatch)
    monkeypatch.setattr("app.agents.graph.get_db", lambda: _fake_db([{"amount": 10}]))

    gemini = _StubGemini(
        query_result=QuerySpec(collection="orders", operation="find"), answer="1 order"
    )

    with caplog.at_level(logging.INFO, logger="audit"):
        answer_question("how many orders?", gemini)

    event = caplog.records[-1].event
    assert {"classify_ms", "orders_agent_ms", "synthesize_ms", "graph_ms"} <= set(event["timings"])
    assert all(value >= 0 for value in event["timings"].values())


def test_query_generation_failure_still_returns_a_graceful_message(monkeypatch, caplog):
    _patch_shared_caches(monkeypatch)

    class _SlowFailingGemini:
        def generate_structured(self, prompt, schema):
            raise TimeoutError("The read operation timed out")

    with caplog.at_level(logging.INFO, logger="audit"):
        result = answer_question("how many cash orders?", _SlowFailingGemini())

    assert "Something went wrong" in result.text
    event = caplog.records[-1].event
    assert "graph_ms" in event["timings"]


def test_rate_limit_error_gives_a_clean_message_not_the_raw_api_payload(monkeypatch):
    from google.genai import errors

    _patch_shared_caches(monkeypatch)

    class _RateLimitedGemini:
        def generate_structured(self, prompt, schema):
            raise errors.ClientError(
                429,
                {
                    "error": {
                        "code": 429,
                        "message": "Quota exceeded for metric: generate_content_free_tier_requests",
                        "status": "RESOURCE_EXHAUSTED",
                    }
                },
            )

    result = answer_question("how many orders?", _RateLimitedGemini())

    assert "rate-limited" in result.text
    assert "RESOURCE_EXHAUSTED" not in result.text
    assert "generate_content_free_tier_requests" not in result.text


def test_answer_generation_failure_is_handled_gracefully(monkeypatch, caplog):
    """generate_answer raising should surface as a graceful top-level message instead of an
    unhandled exception escaping answer_question."""
    _patch_shared_caches(monkeypatch)
    monkeypatch.setattr("app.agents.graph.get_db", lambda: _fake_db([{"amount": 10}]))

    class _FailsOnAnswerGemini(_StubGemini):
        def generate_answer(self, question, rows_by_domain, skipped_domains=None):
            raise TimeoutError("The read operation timed out")

    with caplog.at_level(logging.INFO, logger="audit"):
        result = answer_question(
            "how many orders?",
            _FailsOnAnswerGemini(query_result=QuerySpec(collection="orders", operation="find")),
        )

    assert "Something went wrong" in result.text


def test_repeated_question_in_same_channel_is_served_from_cache(monkeypatch):
    _patch_shared_caches(monkeypatch, ttl_seconds=60, max_entries=10)
    monkeypatch.setattr("app.agents.graph.get_db", lambda: _fake_db([{"amount": 10}]))

    gemini = _StubGemini(
        query_result=QuerySpec(collection="orders", operation="find"), answer="cached answer"
    )

    first = answer_question("How many orders?", gemini, channel_id="C1")
    second = answer_question("  how many orders?  ", gemini, channel_id="C1")

    assert first.text == second.text == "cached answer"
    assert gemini.query_spec_calls == 1
    assert len(gemini.answer_calls) == 1


def test_cache_is_scoped_per_channel(monkeypatch):
    _patch_shared_caches(monkeypatch, ttl_seconds=60, max_entries=10)
    monkeypatch.setattr("app.agents.graph.get_db", lambda: _fake_db([{"amount": 10}]))

    gemini = _StubGemini(
        query_result=QuerySpec(collection="orders", operation="find"), answer="answer"
    )

    answer_question("how many orders?", gemini, channel_id="C1")
    answer_question("how many orders?", gemini, channel_id="C2")

    assert gemini.query_spec_calls == 2


def test_cache_hit_logs_cache_hit_true(monkeypatch, caplog):
    _patch_shared_caches(monkeypatch, ttl_seconds=60, max_entries=10)
    monkeypatch.setattr("app.agents.graph.get_db", lambda: _fake_db([{"amount": 10}]))

    gemini = _StubGemini(
        query_result=QuerySpec(collection="orders", operation="find"), answer="answer"
    )

    with caplog.at_level(logging.INFO, logger="audit"):
        answer_question("how many orders?", gemini, channel_id="C1")
        first_event = caplog.records[-1].event
        answer_question("how many orders?", gemini, channel_id="C1")
        second_event = caplog.records[-1].event

    assert first_event["cache_hit"] is False
    assert second_event["cache_hit"] is True


def test_quota_exceeded_responses_are_not_cached(monkeypatch):
    _patch_shared_caches(monkeypatch, ttl_seconds=60, max_entries=10)
    monkeypatch.setattr("app.rag.pipeline.quota_tracker.is_over_budget", lambda: True)
    gemini = _StubGemini(query_result=QuerySpec(collection="orders", operation="find"))

    answer_question("how many orders?", gemini, channel_id="C1")
    answer_question("how many orders?", gemini, channel_id="C1")

    assert gemini.query_spec_calls == 0


def test_query_error_responses_are_cached(monkeypatch):
    _patch_shared_caches(monkeypatch, ttl_seconds=60, max_entries=10)
    gemini = _StubGemini(query_result=QueryError(error="I don't have data for that"))

    first = answer_question("what's the weather?", gemini, channel_id="C1")
    second = answer_question("what's the weather?", gemini, channel_id="C1")

    assert first.text == second.text == "I don't have data for that"
    assert gemini.query_spec_calls == 1


def test_no_rows_found_responses_are_cached(monkeypatch):
    _patch_shared_caches(monkeypatch, ttl_seconds=60, max_entries=10)
    monkeypatch.setattr("app.agents.graph.get_db", lambda: _fake_db([]))
    gemini = _StubGemini(query_result=QuerySpec(collection="orders", operation="find"))

    answer_question("orders from Mars?", gemini, channel_id="C1")
    answer_question("orders from Mars?", gemini, channel_id="C1")

    assert gemini.query_spec_calls == 1


def test_gemini_exceptions_are_not_cached(monkeypatch):
    _patch_shared_caches(monkeypatch, ttl_seconds=60, max_entries=10)

    class _AlwaysFailingGemini:
        def __init__(self):
            self.calls = 0

        def generate_structured(self, prompt, schema):
            self.calls += 1
            raise TimeoutError("The read operation timed out")

    gemini = _AlwaysFailingGemini()

    answer_question("how many cash orders?", gemini, channel_id="C1")
    answer_question("how many cash orders?", gemini, channel_id="C1")

    assert gemini.calls == 2


def test_low_confidence_classification_asks_a_clarifying_question(monkeypatch):
    _patch_shared_caches(monkeypatch)
    gemini = _StubGemini(
        domains=[], confidence=0.1, clarification_question="which one do you mean?"
    )

    result = answer_question("show me active ones nearby", gemini, channel_id="C1", user_id="U1")

    assert result.text == "which one do you mean?"


def test_clarification_followup_is_merged_with_original_question(monkeypatch):
    _patch_shared_caches(monkeypatch)
    monkeypatch.setattr("app.agents.graph.get_db", lambda: _fake_db([{"amount": 10}]))

    seen_prompts = []

    class _ClarifyThenAnswerGemini(_StubGemini):
        def generate_structured(self, prompt, schema):
            seen_prompts.append(prompt)
            if len(seen_prompts) == 1:
                return Classification(
                    domains=[],
                    confidence=0.1,
                    needs_geo=False,
                    clarification_question="active what -- customers or vendors?",
                )
            return Classification(domains=["orders"], confidence=0.9, needs_geo=False)

    gemini = _ClarifyThenAnswerGemini(query_result=QuerySpec(collection="orders", operation="find"))

    first = answer_question("show me active ones", gemini, channel_id="C1", user_id="U1")
    assert first.text == "active what -- customers or vendors?"

    second = answer_question("orders", gemini, channel_id="C1", user_id="U1")
    assert "the answer" in second.text
    # The follow-up's classification call should have seen both the original question and the
    # follow-up merged together, not just "orders" in isolation.
    assert "show me active ones" in seen_prompts[-1]
    assert "orders" in seen_prompts[-1]


def test_reset_command_clears_clarification_conversation_and_switch_state(monkeypatch):
    _patch_shared_caches(monkeypatch)

    from app.rag import pipeline as pipeline_module

    key = ("C1", "U1")
    pipeline_module.clarification_cache.set(
        key, PendingClarification(original_question="show me active ones", rounds=1)
    )
    pipeline_module.conversation_context_cache.set(
        key, "how many orders did vendor V1 have last week?"
    )
    pipeline_module.context_switch_cache.set(
        key, PendingContextSwitch(candidate_question="how many orders were placed today?")
    )

    class _ExplodingGemini:
        def generate_structured(self, prompt, schema):
            raise AssertionError("classify should not run for a reset command")

    result = answer_question("reset", _ExplodingGemini(), channel_id="C1", user_id="U1")

    assert "cleared our conversation context" in result.text
    assert pipeline_module.clarification_cache.get(key) is None
    assert pipeline_module.conversation_context_cache.get(key) is None
    assert pipeline_module.context_switch_cache.get(key) is None


@pytest.mark.parametrize(
    "phrase",
    ["Reset", "  reset  ", "RESET!", "new topic", "New Topic?", "start over.", "Forget That"],
)
def test_reset_command_recognizes_common_phrasings(monkeypatch, phrase):
    _patch_shared_caches(monkeypatch)

    class _ExplodingGemini:
        def generate_structured(self, prompt, schema):
            raise AssertionError("classify should not run for a reset command")

    result = answer_question(phrase, _ExplodingGemini(), channel_id="C1", user_id="U1")

    assert "cleared our conversation context" in result.text


def test_reset_command_bypasses_rate_limiter_and_quota(monkeypatch):
    _patch_shared_caches(monkeypatch)
    monkeypatch.setattr(rate_limiter, "_limit", 1)
    monkeypatch.setattr("app.rag.pipeline.quota_tracker.is_over_budget", lambda: True)
    # Exhaust the one allowed slot so a normal question would now be rate-limited.
    rate_limiter.allow(rate_limiter.make_key("C1", "U1"))

    class _ExplodingGemini:
        def generate_structured(self, prompt, schema):
            raise AssertionError("classify should not run for a reset command")

    result = answer_question("reset", _ExplodingGemini(), channel_id="C1", user_id="U1")

    assert "cleared our conversation context" in result.text


def test_reset_command_is_never_served_from_or_written_to_answer_cache(monkeypatch):
    _patch_shared_caches(monkeypatch)

    from app.rag import pipeline as pipeline_module

    class _ExplodingGemini:
        def generate_structured(self, prompt, schema):
            raise AssertionError("classify should not run for a reset command")

    answer_question("reset", _ExplodingGemini(), channel_id="C1", user_id="U1")

    cache_key = pipeline_module.answer_cache.make_key("C1", "reset")
    assert pipeline_module.answer_cache.get(cache_key) is None


def test_message_merely_containing_reset_word_is_not_treated_as_a_reset_command(monkeypatch):
    _patch_shared_caches(monkeypatch)
    monkeypatch.setattr("app.agents.graph.get_db", lambda: _fake_db([{"amount": 10}]))

    gemini = _StubGemini(
        query_result=QuerySpec(collection="orders", operation="find"),
        answer="12 orders were reset",
    )
    result = answer_question(
        "how many orders did we reset last week?", gemini, channel_id="C1", user_id="U1"
    )

    assert result.text == "12 orders were reset"
    assert gemini.query_spec_calls == 1


def test_followup_question_reuses_previous_turn_context(monkeypatch):
    """A short elliptical second question ("what about the total amount?") has no subject of its
    own -- the classifier must be shown the first turn's resolved question and, per
    Classification.context_mode, fold it into a self-contained resolved_question that the domain
    agent actually generates and answers against, not the bare fragment."""
    _patch_shared_caches(monkeypatch)
    monkeypatch.setattr("app.agents.graph.get_db", lambda: _fake_db([{"amount": 10}]))

    seen_prompts = []

    class _ContextAwareGemini:
        def generate_structured(self, prompt, schema):
            seen_prompts.append(prompt)
            # Matched on the exact trailing "Question: <text>" line (the literal last line of the
            # classify prompt template) rather than a loose substring -- the prompt's own
            # baked-in few-shot examples happen to use similar phrasing, so a loose `in prompt`
            # check would match on every call regardless of what's actually being classified.
            if prompt.rstrip().endswith("Question: what about the grand total?"):
                return Classification(
                    domains=["orders"],
                    confidence=0.9,
                    context_mode="followup",
                    resolved_question="what is the grand total of orders vendor V1 had last week?",
                )
            return Classification(
                domains=["orders"],
                confidence=0.9,
                resolved_question="how many orders did vendor V1 have last week?",
            )

        def generate_structured_or_error(self, prompt, schema):
            return QuerySpec(collection="orders", operation="find")

        def generate_answer(self, question, rows_by_domain, skipped_domains=None):
            return f"answered: {question}"

    gemini = _ContextAwareGemini()

    first = answer_question(
        "how many orders did vendor V1 have last week?", gemini, channel_id="C1", user_id="U1"
    )
    assert first.text == "answered: how many orders did vendor V1 have last week?"

    second = answer_question("what about the grand total?", gemini, channel_id="C1", user_id="U1")

    assert second.text == "answered: what is the grand total of orders vendor V1 had last week?"
    # The second turn's classify prompt must have seen the first turn's resolved question.
    assert "how many orders did vendor V1 have last week?" in seen_prompts[-1]


def test_unrelated_followup_question_asks_for_confirmation_before_answering(monkeypatch):
    """The classifier sees the previous turn's question on every message with cached context
    (a one-line prompt addition), but an unrelated question must default to context_mode
    "new_topic" -- and rather than silently answering it, the graph routes to
    confirm_context_switch (see app/agents/graph.py), so the user gets asked before that context
    is discarded. No query is generated for the candidate question until confirmed."""
    _patch_shared_caches(monkeypatch)
    monkeypatch.setattr("app.agents.graph.get_db", lambda: _fake_db([{"amount": 10}]))

    class _ContextAwareGemini:
        def generate_structured(self, prompt, schema):
            # See the tail-anchored match note in
            # test_followup_question_reuses_previous_turn_context above -- this question stays in
            # the "orders" domain deliberately, since a loose substring check against a
            # domain-changing question would collide with this prompt's own baked-in cross-domain
            # examples.
            if prompt.rstrip().endswith("Question: how many orders were placed today?"):
                return Classification(domains=["orders"], confidence=0.9)
            return Classification(
                domains=["orders"],
                confidence=0.9,
                resolved_question="how many orders did vendor V1 have last week?",
            )

        def generate_structured_or_error(self, prompt, schema):
            raise AssertionError("no query should be generated before the switch is confirmed")

        def generate_answer(self, question, rows_by_domain, skipped_domains=None):
            raise AssertionError("no answer should be synthesized before the switch is confirmed")

    gemini = _ContextAwareGemini()
    answer_question(
        "how many orders did vendor V1 have last week?", gemini, channel_id="C1", user_id="U1"
    )
    result = answer_question(
        "how many orders were placed today?", gemini, channel_id="C1", user_id="U1"
    )

    assert "how many orders did vendor V1 have last week?" in result.text
    assert "yes or no" in result.text.lower()


def test_confirming_a_context_switch_answers_the_candidate_question_fresh(monkeypatch):
    _patch_shared_caches(monkeypatch)
    monkeypatch.setattr("app.agents.graph.get_db", lambda: _fake_db([{"amount": 10}]))

    class _ContextAwareGemini:
        def generate_structured(self, prompt, schema):
            if prompt.rstrip().endswith("Question: how many orders were placed today?"):
                return Classification(domains=["orders"], confidence=0.9)
            return Classification(
                domains=["orders"],
                confidence=0.9,
                resolved_question="how many orders did vendor V1 have last week?",
            )

        def generate_structured_or_error(self, prompt, schema):
            return QuerySpec(collection="orders", operation="find")

        def generate_answer(self, question, rows_by_domain, skipped_domains=None):
            return f"answered: {question}"

    gemini = _ContextAwareGemini()
    answer_question(
        "how many orders did vendor V1 have last week?", gemini, channel_id="C1", user_id="U1"
    )
    answer_question("how many orders were placed today?", gemini, channel_id="C1", user_id="U1")

    result = answer_question("yes", gemini, channel_id="C1", user_id="U1")

    assert result.text == "answered: how many orders were placed today?"


def test_declining_a_context_switch_keeps_the_old_context_and_asks_nothing_of_gemini(monkeypatch):
    _patch_shared_caches(monkeypatch)
    monkeypatch.setattr("app.agents.graph.get_db", lambda: _fake_db([{"amount": 10}]))

    class _ContextAwareGemini:
        def generate_structured(self, prompt, schema):
            if prompt.rstrip().endswith("Question: how many orders were placed today?"):
                return Classification(domains=["orders"], confidence=0.9)
            return Classification(
                domains=["orders"],
                confidence=0.9,
                resolved_question="how many orders did vendor V1 have last week?",
            )

        def generate_structured_or_error(self, prompt, schema):
            return QuerySpec(collection="orders", operation="find")

        def generate_answer(self, question, rows_by_domain, skipped_domains=None):
            return f"answered: {question}"

    gemini = _ContextAwareGemini()
    answer_question(
        "how many orders did vendor V1 have last week?", gemini, channel_id="C1", user_id="U1"
    )
    answer_question("how many orders were placed today?", gemini, channel_id="C1", user_id="U1")

    result = answer_question("no", gemini, channel_id="C1", user_id="U1")

    assert "sticking with our current conversation" in result.text

    from app.rag import pipeline as pipeline_module

    # Declining drops only the pending confirmation -- the original context survives for a real
    # follow-up.
    assert pipeline_module.context_switch_cache.get(("C1", "U1")) is None
    assert (
        pipeline_module.conversation_context_cache.get(("C1", "U1"))
        == "how many orders did vendor V1 have last week?"
    )


def test_ambiguous_reply_to_a_context_switch_prompt_is_treated_as_a_fresh_message(monkeypatch):
    """Neither "yes" nor "no" -- the stale prompt is dropped (no infinite nagging) and the new
    message is classified normally on its own merits."""
    _patch_shared_caches(monkeypatch)
    monkeypatch.setattr("app.agents.graph.get_db", lambda: _fake_db([{"amount": 10}]))

    class _ContextAwareGemini:
        def generate_structured(self, prompt, schema):
            if prompt.rstrip().endswith("Question: how many orders were placed today?"):
                return Classification(domains=["orders"], confidence=0.9)
            if prompt.rstrip().endswith("Question: how many pending orders are there?"):
                return Classification(domains=["orders"], confidence=0.9)
            return Classification(
                domains=["orders"],
                confidence=0.9,
                resolved_question="how many orders did vendor V1 have last week?",
            )

        def generate_structured_or_error(self, prompt, schema):
            return QuerySpec(collection="orders", operation="find")

        def generate_answer(self, question, rows_by_domain, skipped_domains=None):
            return f"answered: {question}"

    gemini = _ContextAwareGemini()
    answer_question(
        "how many orders did vendor V1 have last week?", gemini, channel_id="C1", user_id="U1"
    )
    answer_question("how many orders were placed today?", gemini, channel_id="C1", user_id="U1")

    result = answer_question(
        "how many pending orders are there?", gemini, channel_id="C1", user_id="U1"
    )

    assert result.text == "answered: how many pending orders are there?"

    from app.rag import pipeline as pipeline_module

    assert pipeline_module.context_switch_cache.get(("C1", "U1")) is None


def test_pending_clarification_does_not_also_consult_conversation_context(monkeypatch):
    """A pending clarification already carries context forward via its own string-merge
    (app/rag/clarification_cache.py) -- conversation context must not be layered on top of that,
    i.e. previous_question stays None while a clarification round-trip is in progress."""
    _patch_shared_caches(monkeypatch)
    monkeypatch.setattr("app.agents.graph.get_db", lambda: _fake_db([{"amount": 10}]))

    from app.rag import pipeline as pipeline_module

    pipeline_module.conversation_context_cache.set(("C1", "U1"), "totally unrelated prior context")

    seen_prompts = []

    class _ClarifyThenAnswerGemini(_StubGemini):
        def generate_structured(self, prompt, schema):
            seen_prompts.append(prompt)
            if len(seen_prompts) == 1:
                return Classification(
                    domains=[], confidence=0.1, clarification_question="which one?"
                )
            return Classification(domains=["orders"], confidence=0.9)

    gemini = _ClarifyThenAnswerGemini(query_result=QuerySpec(collection="orders", operation="find"))

    answer_question("show me active ones", gemini, channel_id="C1", user_id="U1")
    answer_question("orders", gemini, channel_id="C1", user_id="U1")

    assert "totally unrelated prior context" not in seen_prompts[-1]


def test_circuit_breaker_open_gives_a_clean_message_not_the_raw_exception(monkeypatch):
    from app.llm.circuit_breaker import CircuitBreakerOpenError

    _patch_shared_caches(monkeypatch)

    class _BrokenCircuitGemini:
        def generate_structured(self, prompt, schema):
            raise CircuitBreakerOpenError("Gemini circuit breaker open after 5 failures")

    result = answer_question("how many orders?", _BrokenCircuitGemini())

    assert "can't reach the service" in result.text


def test_rate_limited_user_gets_a_clean_message_without_reaching_gemini(monkeypatch):
    _patch_shared_caches(monkeypatch)
    monkeypatch.setattr(rate_limiter, "_limit", 1)

    gemini = _StubGemini()
    first = answer_question("first question", gemini, channel_id="C1", user_id="U1")
    assert first.text == "the answer"

    second = answer_question("second question", gemini, channel_id="C1", user_id="U1")

    assert "asking faster" in second.text
    assert gemini.query_spec_calls == 1  # the second call never reached Gemini


def test_rate_limit_is_scoped_per_user_not_shared_across_the_channel(monkeypatch):
    _patch_shared_caches(monkeypatch)
    monkeypatch.setattr(rate_limiter, "_limit", 1)

    gemini = _StubGemini()
    answer_question("q from u1", gemini, channel_id="C1", user_id="U1")
    other_user = answer_question("q from u2", gemini, channel_id="C1", user_id="U2")

    assert other_user.text == "the answer"


def test_clarification_gives_up_after_max_rounds(monkeypatch):
    _patch_shared_caches(monkeypatch)
    monkeypatch.setattr("app.config.settings.agent_max_clarification_rounds", 1)

    gemini = _StubGemini(domains=[], confidence=0.1, clarification_question="which one?")

    answer_question("vague question", gemini, channel_id="C1", user_id="U1")
    final = answer_question("still vague", gemini, channel_id="C1", user_id="U1")

    assert "still don't have enough information" in final.text
