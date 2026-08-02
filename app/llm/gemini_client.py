"""Thin wrapper around Gemini for query generation and answer formatting."""

import json
import random
import re
import time
from collections.abc import Callable
from typing import TypeVar

from google import genai
from google.genai import errors

from app.config import settings
from app.llm.quota import quota_tracker
from app.rag.query_spec import QueryError, QuerySpec

T = TypeVar("T")
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

_QUERY_PROMPT = """You translate questions about a MongoDB database into a single structured query.

Schema:
{schema_context}

Rules:
- Only use the collections shown above.
- Respond with ONLY a JSON object, no prose, no markdown fences.
- If the question cannot be answered from this schema, respond with: {{"error": "<short reason>"}}
- Otherwise respond with an object matching this shape:
  {{
    "collection": "<collection name>",
    "operation": "find" | "aggregate" | "count",
    "filter": {{}},        // for "find" and "count"
    "pipeline": [],        // for "aggregate", a list of aggregation stages
    "projection": null,    // optional, for "find"
    "sort": null,          // optional, for "find", e.g. {{"field": -1}}
    "limit": 50
  }}
- Never use $where, $function, $accumulator, $merge, or $out.

Question: {question}
"""

_ANSWER_PROMPT = """Answer the user's question using only the data below. Be concise and factual.
If the data doesn't fully answer the question, say so.

Question: {question}

Data (JSON):
{rows}

Answer:
"""


def _extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"Gemini response did not contain JSON: {text!r}")
    return json.loads(match.group(0))


def _is_retryable(exc: Exception) -> bool:
    return isinstance(exc, errors.APIError) and exc.code in _RETRYABLE_STATUS_CODES


class GeminiClient:
    def __init__(self, model_name: str | None = None) -> None:
        self.client = genai.Client(api_key=settings.gemini_api_key)
        self.model_name = model_name or settings.gemini_model
        self.max_retries = settings.gemini_max_retries
        self.retry_base_delay_seconds = settings.gemini_retry_base_delay_seconds

    def _call_with_retry(self, fn: Callable[[], T]) -> T:
        quota_tracker.record_call()
        attempt = 0
        while True:
            try:
                return fn()
            except Exception as exc:
                if attempt >= self.max_retries or not _is_retryable(exc):
                    raise
                jitter = random.uniform(0, 0.5)  # nosec B311 - retry backoff jitter, not security-sensitive
                delay = self.retry_base_delay_seconds * (2**attempt) + jitter
                time.sleep(delay)
                attempt += 1

    def generate_query_spec(self, question: str, schema_context: str) -> QuerySpec | QueryError:
        prompt = _QUERY_PROMPT.format(schema_context=schema_context, question=question)
        response = self._call_with_retry(
            lambda: self.client.models.generate_content(model=self.model_name, contents=prompt)
        )
        data = _extract_json(response.text)
        if "error" in data:
            return QueryError(**data)
        return QuerySpec(**data)

    def generate_answer(self, question: str, rows: list[dict]) -> str:
        prompt = _ANSWER_PROMPT.format(question=question, rows=json.dumps(rows, default=str))
        response = self._call_with_retry(
            lambda: self.client.models.generate_content(model=self.model_name, contents=prompt)
        )
        return response.text.strip()
