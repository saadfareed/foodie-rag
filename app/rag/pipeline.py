"""End-to-end: question -> generated query -> validated -> executed -> answer."""

import time
from collections.abc import Iterator
from contextlib import contextmanager

from app.audit.logger import log_query_event
from app.config import settings
from app.db.executor import execute_query_spec
from app.db.mongo import get_db
from app.llm.gemini_client import GeminiClient
from app.llm.quota import quota_tracker
from app.rag.query_spec import QueryError
from app.rag.schema_context import build_schema_context
from app.rag.validator import QueryValidationError, validate_query_spec


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
    gemini = gemini or GeminiClient()
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

    if quota_tracker.is_over_budget():
        answer = "I've hit my daily question budget -- please try again tomorrow."
        _log(error="daily_budget_exceeded", answer=answer)
        return answer

    with _timed_stage(timings, "schema_context_ms"):
        schema_context = build_schema_context()

    try:
        with _timed_stage(timings, "query_gen_ms"):
            spec_or_error = gemini.generate_query_spec(question, schema_context)
    except Exception as exc:
        answer = f"Sorry, I couldn't process that question right now ({exc})."
        _log(error=str(exc), answer=answer)
        return answer

    if isinstance(spec_or_error, QueryError):
        _log(error=spec_or_error.error, answer=spec_or_error.error)
        return spec_or_error.error

    try:
        spec = validate_query_spec(spec_or_error, settings.mongodb_allowed_collections)
    except QueryValidationError as exc:
        answer = f"I can't run that query: {exc}"
        _log(spec=spec_or_error, error=str(exc), answer=answer)
        return answer

    try:
        with _timed_stage(timings, "db_ms"):
            rows = execute_query_spec(get_db(), spec, timeout_ms=settings.mongodb_query_timeout_ms)
    except Exception as exc:
        answer = f"I ran into a database error answering that: {exc}"
        _log(spec=spec, error=str(exc), answer=answer)
        return answer

    if not rows:
        answer = "I didn't find any data matching that question."
        _log(spec=spec, row_count=0, answer=answer)
        return answer

    try:
        with _timed_stage(timings, "answer_gen_ms"):
            answer = gemini.generate_answer(question, rows)
    except Exception as exc:
        answer = f"I found the data but couldn't put it into words just now ({exc})."
        _log(spec=spec, row_count=len(rows), error=str(exc), answer=answer)
        return answer

    _log(spec=spec, row_count=len(rows), answer=answer)
    return answer
