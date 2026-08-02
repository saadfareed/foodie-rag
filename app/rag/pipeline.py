"""End-to-end: question -> generated query -> validated -> executed -> answer."""

import time

from app.audit.logger import log_query_event
from app.config import settings
from app.db.executor import execute_query_spec
from app.db.mongo import get_db
from app.llm.gemini_client import GeminiClient
from app.llm.quota import quota_tracker
from app.rag.query_spec import QueryError
from app.rag.schema_context import build_schema_context
from app.rag.validator import QueryValidationError, validate_query_spec


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

    def _mark(name: str, stage_start: float) -> None:
        timings[name] = round((time.perf_counter() - stage_start) * 1000, 2)

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

    stage_start = time.perf_counter()
    schema_context = build_schema_context()
    _mark("schema_context_ms", stage_start)

    stage_start = time.perf_counter()
    try:
        spec_or_error = gemini.generate_query_spec(question, schema_context)
    except Exception as exc:
        answer = f"Sorry, I couldn't process that question right now ({exc})."
        _log(error=str(exc), answer=answer)
        return answer
    finally:
        _mark("query_gen_ms", stage_start)

    if isinstance(spec_or_error, QueryError):
        _log(error=spec_or_error.error, answer=spec_or_error.error)
        return spec_or_error.error

    try:
        spec = validate_query_spec(spec_or_error, settings.mongodb_allowed_collections)
    except QueryValidationError as exc:
        answer = f"I can't run that query: {exc}"
        _log(spec=spec_or_error, error=str(exc), answer=answer)
        return answer

    stage_start = time.perf_counter()
    try:
        rows = execute_query_spec(get_db(), spec, timeout_ms=settings.mongodb_query_timeout_ms)
    except Exception as exc:
        answer = f"I ran into a database error answering that: {exc}"
        _log(spec=spec, error=str(exc), answer=answer)
        return answer
    finally:
        _mark("db_ms", stage_start)

    if not rows:
        answer = "I didn't find any data matching that question."
        _log(spec=spec, row_count=0, answer=answer)
        return answer

    stage_start = time.perf_counter()
    answer = gemini.generate_answer(question, rows)
    _mark("answer_gen_ms", stage_start)
    _log(spec=spec, row_count=len(rows), answer=answer)
    return answer
