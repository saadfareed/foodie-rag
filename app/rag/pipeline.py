"""End-to-end: question -> generated query -> validated -> executed -> answer."""

from app.config import settings
from app.db.executor import execute_query_spec
from app.db.mongo import get_db
from app.llm.gemini_client import GeminiClient
from app.rag.query_spec import QueryError
from app.rag.schema_context import build_schema_context
from app.rag.validator import QueryValidationError, validate_query_spec


def answer_question(question: str, gemini: GeminiClient | None = None) -> str:
    gemini = gemini or GeminiClient()
    schema_context = build_schema_context()

    try:
        spec_or_error = gemini.generate_query_spec(question, schema_context)
    except Exception as exc:
        return f"Sorry, I couldn't process that question right now ({exc})."

    if isinstance(spec_or_error, QueryError):
        return spec_or_error.error

    try:
        spec = validate_query_spec(spec_or_error, settings.mongodb_allowed_collections)
    except QueryValidationError as exc:
        return f"I can't run that query: {exc}"

    try:
        rows = execute_query_spec(get_db(), spec, timeout_ms=settings.mongodb_query_timeout_ms)
    except Exception as exc:
        return f"I ran into a database error answering that: {exc}"

    if not rows:
        return "I didn't find any data matching that question."

    return gemini.generate_answer(question, rows)
