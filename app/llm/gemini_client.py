"""Thin wrapper around Gemini for query generation and answer formatting."""

import json
import random
import re
import time
from collections.abc import Callable
from typing import TypeVar

import httpx
from google import genai
from google.genai import errors, types

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
    "limit": 50,
    "start_date": null,    // optional, "YYYY-MM-DD" -- lower date bound, if any, in filter/pipeline
    "end_date": null       // optional, "YYYY-MM-DD" -- upper date bound, if any, in filter/pipeline
  }}
- If the question implies a date range, restrict it in filter/pipeline as usual AND also set
  start_date/end_date to describe that same range -- they must agree with the actual filter.
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
    if isinstance(exc, errors.APIError):
        return exc.code in _RETRYABLE_STATUS_CODES
    # A client-side read/connect timeout is transient by nature -- worth a retry rather than an
    # immediate hard failure. Without this, a single slow response fails the whole question with
    # no retry at all (this is what happened in production: a 15s HTTP timeout on the
    # query-generation call raised httpx.ReadTimeout, which wasn't a google.genai.errors.APIError,
    # so _call_with_retry gave up on the first attempt instead of retrying).
    return isinstance(exc, httpx.TimeoutException)


def _rows_for_prompt(rows: list[dict], max_rows: int) -> str:
    """Cap the rows serialized into the answer prompt so payload size (and Gemini latency)
    stays bounded regardless of how many rows the query returned."""
    if len(rows) <= max_rows:
        return json.dumps(rows, default=str)
    kept = json.dumps(rows[:max_rows], default=str)
    return f"{kept}\n(...{len(rows) - max_rows} more row(s) omitted for brevity...)"


class GeminiClient:
    def __init__(self, model_name: str | None = None) -> None:
        self.client = genai.Client(
            api_key=settings.gemini_api_key,
            http_options=types.HttpOptions(timeout=settings.gemini_request_timeout_ms),
        )
        self.model_name = model_name or settings.gemini_model
        self.max_retries = settings.gemini_max_retries
        self.retry_base_delay_seconds = settings.gemini_retry_base_delay_seconds
        self.max_retry_seconds = settings.gemini_max_retry_seconds
        self.answer_max_rows = settings.gemini_answer_max_rows
        # Query generation is deterministic structured extraction, not open-ended reasoning:
        # temperature=0 for consistent output, response_mime_type="application/json" to skip
        # prose padding, and thinking disabled (thinking_budget=0) since on "thinking" models
        # reasoning tokens otherwise count against max_output_tokens -- this previously truncated
        # the JSON mid-object in production (a 512-token cap was consumed by invisible reasoning
        # before the model finished emitting the closing brace) while contributing most of that
        # call's ~14s latency. Disabling thinking fixes both the correctness bug and the latency.
        self.query_generation_config = types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            max_output_tokens=settings.gemini_query_max_output_tokens,
            thinking_config=types.ThinkingConfig(
                thinking_budget=settings.gemini_query_thinking_budget
            ),
        )

    def _call_with_retry(self, fn: Callable[[], T]) -> T:
        quota_tracker.record_call()
        attempt = 0
        start = time.monotonic()
        while True:
            try:
                return fn()
            except Exception as exc:
                if attempt >= self.max_retries or not _is_retryable(exc):
                    raise
                jitter = random.uniform(0, 0.5)  # nosec B311 - retry backoff jitter, not security-sensitive
                delay = self.retry_base_delay_seconds * (2**attempt) + jitter
                if time.monotonic() - start + delay > self.max_retry_seconds:
                    raise
                time.sleep(delay)
                attempt += 1

    def generate_query_spec(self, question: str, schema_context: str) -> QuerySpec | QueryError:
        prompt = _QUERY_PROMPT.format(schema_context=schema_context, question=question)
        response = self._call_with_retry(
            lambda: self.client.models.generate_content(
                model=self.model_name, contents=prompt, config=self.query_generation_config
            )
        )
        data = _extract_json(response.text)
        if "error" in data:
            return QueryError(**data)
        return QuerySpec(**data)

    def generate_answer(self, question: str, rows: list[dict]) -> str:
        prompt = _ANSWER_PROMPT.format(
            question=question, rows=_rows_for_prompt(rows, self.answer_max_rows)
        )
        response = self._call_with_retry(
            lambda: self.client.models.generate_content(model=self.model_name, contents=prompt)
        )
        return response.text.strip()
