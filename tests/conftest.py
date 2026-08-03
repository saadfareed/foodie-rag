import pytest

from app.llm.circuit_breaker import gemini_circuit_breaker
from app.llm.quota import quota_tracker
from app.rag.answer_cache import AnswerCache
from app.rag.context_switch_cache import ContextSwitchCache
from app.rag.conversation_context import ConversationContextCache
from app.rag.rate_limiter import rate_limiter


@pytest.fixture(autouse=True)
def _isolated_answer_cache(monkeypatch):
    """app.rag.pipeline.answer_cache is a module-level singleton; without this, tests that reuse
    the same question text (with the default channel_id=None) would leak cached answers into each
    other depending on test order."""
    monkeypatch.setattr(
        "app.rag.pipeline.answer_cache", AnswerCache(ttl_seconds=1800, max_entries=500)
    )


@pytest.fixture(autouse=True)
def _isolated_conversation_context_cache(monkeypatch):
    """app.rag.pipeline.conversation_context_cache is a module-level singleton; without this,
    tests exercising the default (channel_id=None, user_id=None) key could leak a resolved_question
    into an unrelated test's classify prompt, depending on test order."""
    monkeypatch.setattr(
        "app.rag.pipeline.conversation_context_cache",
        ConversationContextCache(ttl_seconds=300, max_entries=500),
    )


@pytest.fixture(autouse=True)
def _isolated_context_switch_cache(monkeypatch):
    """app.rag.pipeline.context_switch_cache is a module-level singleton; without this, a pending
    "should I clear that context?" confirmation from one test could leak into another test using
    the same default (channel_id=None, user_id=None) key, depending on test order."""
    monkeypatch.setattr(
        "app.rag.pipeline.context_switch_cache",
        ContextSwitchCache(ttl_seconds=120, max_entries=500),
    )


@pytest.fixture(autouse=True)
def _reset_quota_tracker(monkeypatch):
    """quota_tracker (app/llm/quota.py) is a single module-level object, imported by reference
    into both app/llm/gemini_client.py and app/rag/pipeline.py -- patching its *attributes*
    (rather than rebinding the name in one module) resets it everywhere it's held. Without this,
    real calls made across the suite (each _call_with_retry invocation counts one) accumulate
    against whatever GEMINI_DAILY_CALL_BUDGET is configured in the environment, and once that's
    a finite number, tests start tripping "I've hit my daily question budget" depending on
    unrelated tests' call counts and run order -- exactly what a real per-test isolated budget
    should never depend on."""
    monkeypatch.setattr(quota_tracker, "_daily_budget", 0)
    monkeypatch.setattr(quota_tracker, "_calls_today", 0)


@pytest.fixture(autouse=True)
def _reset_rate_limiter(monkeypatch):
    """rate_limiter (app/rag/rate_limiter.py) is disabled by default (limit_per_window=0), so
    this is a no-op for most tests -- it only matters for tests that explicitly enable it via
    monkeypatch, keeping that state from leaking into unrelated tests via run order."""
    monkeypatch.setattr(rate_limiter, "_limit", 0)
    monkeypatch.setattr(rate_limiter, "_calls", type(rate_limiter._calls)())


@pytest.fixture(autouse=True)
def _reset_gemini_circuit_breaker(monkeypatch):
    """gemini_circuit_breaker (app/llm/circuit_breaker.py) is disabled by default here (like
    quota_tracker/rate_limiter above) -- several existing tests intentionally drive GeminiClient
    to fail repeatedly (retry-exhaustion, non-retryable errors), and without this those failures
    would accumulate against the shared singleton across the whole test run and eventually trip
    the breaker for unrelated, later tests."""
    monkeypatch.setattr(gemini_circuit_breaker, "_failure_threshold", 0)
    monkeypatch.setattr(gemini_circuit_breaker, "_consecutive_failures", 0)
    monkeypatch.setattr(gemini_circuit_breaker, "_opened_at", None)
