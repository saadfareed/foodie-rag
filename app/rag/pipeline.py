"""Entry point for answering a question: clarification-cache check -> the multi-domain LangGraph
agent (app/agents/graph.py) -> audit/answer-cache/quota integration.

This module used to run a single-shot "one Gemini call picks a collection and writes a query"
pipeline directly; that's now the graph's job (classify -> per-domain agents -> validate ->
execute -> synthesize). What stays here is everything that isn't specific to any one domain:
the daily call budget gate, the per-channel answer cache, the per-(channel,user) clarification
cache, and structured audit logging.
"""

import time
import weakref
from collections.abc import Iterator
from contextlib import contextmanager

from app.agents.graph import build_graph
from app.audit.logger import log_query_event
from app.config import settings
from app.llm.circuit_breaker import CircuitBreakerOpenError
from app.llm.gemini_client import GeminiClient, is_rate_limited
from app.llm.quota import quota_tracker
from app.rag.answer_cache import CachedResult, answer_cache
from app.rag.clarification_cache import PendingClarification, clarification_cache
from app.rag.rate_limiter import rate_limiter

# Compiling a StateGraph isn't free (~10ms) and the graph's structure only depends on which
# GeminiClient instance it closes over, not on any per-question state -- production always
# reuses one shared GeminiClient (see app/main.py), so caching by identity means that ~10ms is
# paid once per process, not once per Slack message. WeakKeyDictionary (not a plain dict keyed by
# id()) matters here: a plain id()-keyed cache would return a stale graph -- built for a
# *different*, already-garbage-collected GeminiClient -- once id() gets reused, which is exactly
# what happened with the many short-lived stub clients across the test suite. Weak keys expire
# the cache entry along with the object instead.
_graph_cache: "weakref.WeakKeyDictionary[GeminiClient, object]" = weakref.WeakKeyDictionary()


def _get_graph(gemini: GeminiClient):
    graph = _graph_cache.get(gemini)
    if graph is None:
        graph = build_graph(gemini)
        _graph_cache[gemini] = graph
    return graph


@contextmanager
def _timed_stage(timings: dict[str, float], name: str) -> Iterator[None]:
    """Records elapsed ms for `name` into `timings` even if the block raises -- the `finally`
    here runs as the exception unwinds out of the `with` block, i.e. *before* any enclosing
    `except` clause runs, so a failing stage's own duration is still captured in the audit log
    for that failure (not just for successful stages)."""
    start = time.perf_counter()
    try:
        yield
    finally:
        timings[name] = round((time.perf_counter() - start) * 1000, 2)


def answer_question(
    question: str,
    gemini: GeminiClient | None = None,
    *,
    user_id: str | None = None,
    channel_id: str | None = None,
) -> str:
    start = time.perf_counter()
    timings: dict[str, float] = {}

    def _log(**kwargs) -> None:
        log_query_event(
            question=question,
            user_id=user_id,
            channel_id=channel_id,
            duration_ms=(time.perf_counter() - start) * 1000,
            timings=timings,
            **kwargs,
        )

    cache_key = answer_cache.make_key(channel_id, question)
    cached = answer_cache.get(cache_key)
    if cached is not None:
        _log(error=cached.error, answer=cached.answer, cache_hit=True)
        return cached.answer

    # Checked before touching Gemini/Mongo/the shared daily budget at all -- a cache hit above
    # is free and shouldn't count against this user's own rate, but everything past this point
    # spends a real resource one user could otherwise monopolize.
    rate_limit_key = rate_limiter.make_key(channel_id, user_id)
    if not rate_limiter.allow(rate_limit_key):
        answer = "You're asking faster than I can keep up -- please wait a bit and try again."
        _log(error="rate_limited", answer=answer)
        return answer

    gemini = gemini or GeminiClient()

    if quota_tracker.is_over_budget():
        answer = "I've hit my daily question budget -- please try again tomorrow."
        _log(error="daily_budget_exceeded", answer=answer)
        return answer

    # A pending clarification for this (channel, user) means the *previous* answer was itself a
    # clarifying question -- treat this message as the follow-up, not a fresh question, by
    # feeding the classifier the combined text. This is why the cache is keyed by
    # (channel_id, user_id) rather than by question text: it's about one user's specific
    # back-and-forth (see app/rag/clarification_cache.py).
    clarification_key = clarification_cache.make_key(channel_id, user_id)
    pending = clarification_cache.get(clarification_key)
    effective_question = f"{pending.original_question} {question}".strip() if pending else question

    graph = _get_graph(gemini)
    try:
        with _timed_stage(timings, "graph_ms"):
            result = graph.invoke(
                {"question": effective_question, "user_id": user_id, "channel_id": channel_id}
            )
    except Exception as exc:
        if isinstance(exc, CircuitBreakerOpenError):
            answer = "I'm having trouble reaching Gemini right now -- please try again shortly."
        elif is_rate_limited(exc):
            answer = "I'm getting rate-limited by Gemini right now -- please try again shortly."
        else:
            answer = f"Sorry, I couldn't process that question right now ({exc})."
        _log(error=str(exc), answer=answer)
        return answer

    timings.update(result.get("timings", {}))
    answer = result["answer"]

    if result.get("needs_clarification"):
        rounds = (pending.rounds + 1) if pending else 1
        if rounds > settings.agent_max_clarification_rounds:
            clarification_cache.clear(clarification_key)
            answer = (
                "I still don't have enough information to answer that -- could you ask again "
                "with more specifics (e.g. orders, customers, or vendors, and a location if it's "
                "a 'nearby' question)?"
            )
        else:
            clarification_cache.set(
                clarification_key,
                PendingClarification(original_question=effective_question, rounds=rounds),
            )
        _log(error="clarification_needed", answer=answer)
        return answer

    clarification_cache.clear(clarification_key)

    specs = list(result.get("specs_by_domain", {}).values())
    row_count = sum(len(rows) for rows in result.get("rows_by_domain", {}).values())
    errors_by_domain = {
        **(result.get("out_of_scope_by_domain") or {}),
        **(result.get("errors_by_domain") or {}),
    } or None

    _log(specs=specs, row_count=row_count, errors_by_domain=errors_by_domain, answer=answer)
    answer_cache.set(cache_key, CachedResult(answer=answer))
    return answer
