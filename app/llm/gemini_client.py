"""Thin wrapper around Gemini for structured extraction and answer formatting.

Structured extraction (query-spec generation, intent classification, ...) all goes through
generate_structured()/generate_structured_or_error() below -- a single hardened transport
(retry/backoff/timeout/thinking-budget/quota-tracking) that every app/agents/ chain is built on
top of via LangChain prompt composition, rather than each agent owning its own model client.
"""

import json
import random
import re
import time
from collections.abc import Callable
from typing import TypeVar

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel

from app.config import settings
from app.llm.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError, gemini_circuit_breaker
from app.llm.quota import quota_tracker
from app.rag.query_spec import QueryError

R = TypeVar("R")
SchemaT = TypeVar("SchemaT", bound=BaseModel)
# 429 is deliberately NOT here: it's rate-limiting, not a transient server fault, and this app's
# multi-agent graph makes several sequential calls per question against a free-tier quota as low
# as 20 requests/day -- retrying a 429 with our few-second backoff schedule can't possibly
# succeed before that quota resets (Google's own error suggests retrying in ~52s, well past
# gemini_max_retry_seconds), so retrying just burns ~9s of latency for a guaranteed second
# failure. Fail fast instead; see is_rate_limited() below for the caller-facing message.
_RETRYABLE_STATUS_CODES = {500, 502, 503, 504}

_ANSWER_PROMPT = """Answer the user's question using only the data below.

Style: get straight to the answer. 1-3 short sentences, no headers, no bullet lists, no "Domain
status" section, no meta-commentary about what the data doesn't contain -- if a number is
missing, just don't mention that angle instead of explaining why you can't compute it.

Data is grouped by domain (orders / customers / vendors) -- fields with the same name can mean
different things in different domains (e.g. `status` on an order vs. on a vendor account), so
don't conflate them. Only mention a domain if its data actually contributes to the answer; a
domain with no rows is worth a single short clause only when the *absence* itself answers part
of the question (e.g. "no vendors were found nearby") -- otherwise omit it entirely, don't list
it as "no rows".

A domain having no rows does NOT mean the question is unanswerable -- if a different domain's
rows already contain what was asked (e.g. a grouped/summed field per entity, even one you have
to infer the meaning of from its name), use it. Never claim data is missing when the rows below
actually contain it.
{skipped_domains_note}
Question: {question}

Data by domain (JSON):
{rows_by_domain}

Answer:
"""

_SKIPPED_DOMAINS_NOTE = """
Domains that could not be queried: {skipped_domains}. Only mention this if it actually leaves
part of the question unanswered -- if the domains above already fully answer it (e.g. the
question named a domain just to identify records, like "each vendor", and an id from another
domain's data already serves that purpose), say nothing about it.
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


def is_rate_limited(exc: Exception) -> bool:
    """True for a 429 from the Gemini API specifically -- lets callers (app/rag/pipeline.py)
    show a clean, friendly message instead of Google's raw error payload (quota metric names,
    doc links, a nested JSON blob), which otherwise leaks straight into the Slack answer."""
    return isinstance(exc, errors.APIError) and exc.code == 429


def _is_model_unavailable(exc: Exception) -> bool:
    """429 (quota exhausted) and 404 (model retired/not accessible to this account -- confirmed
    in practice: the entire gemini-2.5-* family returns 404 "no longer available to new users"
    for some accounts) both mean *this specific model* can't serve the request right now, unlike
    a 5xx/timeout which isn't model-specific. Both should advance to the next configured
    fallback model in _generate_content(); a 404 is deliberately NOT treated as rate-limited by
    is_rate_limited() above, since the user-facing message for "this model doesn't exist" should
    stay generic rather than claim to be a rate limit."""
    return isinstance(exc, errors.APIError) and exc.code in (429, 404)


def _rows_for_prompt(rows_by_domain: dict[str, list[dict]], max_rows: int) -> str:
    """Cap the rows serialized into the answer prompt so payload size (and Gemini latency) stays
    bounded regardless of how many rows the query returned -- the cap applies per domain, so one
    chatty domain can't crowd the others out of the prompt entirely."""
    capped = {}
    for domain, rows in rows_by_domain.items():
        if len(rows) <= max_rows:
            capped[domain] = rows
        else:
            capped[domain] = [
                *rows[:max_rows],
                f"...{len(rows) - max_rows} more row(s) omitted for brevity...",
            ]
    return json.dumps(capped, default=str)


class GeminiClient:
    def __init__(self, model_name: str | None = None) -> None:
        self.client = genai.Client(
            api_key=settings.gemini_api_key,
            http_options=types.HttpOptions(timeout=settings.gemini_request_timeout_ms),
        )
        self.model_name = model_name or settings.gemini_model
        self.fallback_models = settings.gemini_fallback_models
        self.last_model_used: str | None = None
        # Keyed by model name -- confirmed in production: a single *shared* circuit breaker
        # across every model in the fallback chain means one model's real outage (e.g.
        # gemini-3-flash-preview hitting its daily quota) trips the breaker for everyone,
        # blocking fallback attempts at a *different*, perfectly healthy model
        # (gemini-3.5-flash-lite) too -- defeating the entire point of having a fallback chain.
        # Each model gets its own independent breaker instead; see _breaker_for().
        self._breakers: dict[str, CircuitBreaker] = {}
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

    def _breaker_for(self, model: str) -> CircuitBreaker:
        """Lazily-created, per-model circuit breaker -- see the comment on self._breakers in
        __init__ for why this can't just be the shared gemini_circuit_breaker singleton."""
        breaker = self._breakers.get(model)
        if breaker is None:
            breaker = CircuitBreaker(
                failure_threshold=settings.gemini_circuit_breaker_threshold,
                cooldown_seconds=settings.gemini_circuit_breaker_cooldown_seconds,
            )
            self._breakers[model] = breaker
        return breaker

    def _call_with_retry(self, fn: Callable[[], R], breaker: CircuitBreaker | None = None) -> R:
        # Defaults to the shared module-level breaker only when no per-model breaker is given --
        # every real call site (_generate_content) always passes one explicitly (see
        # _breaker_for). Checked first, before quota_tracker.record_call(): a call the breaker
        # fast-fails never actually reaches Gemini, so it shouldn't be charged against the daily
        # budget either.
        breaker = gemini_circuit_breaker if breaker is None else breaker
        breaker.before_call()
        quota_tracker.record_call()
        attempt = 0
        start = time.monotonic()
        while True:
            try:
                result = fn()
            except Exception as exc:
                if attempt >= self.max_retries or not _is_retryable(exc):
                    breaker.record_failure()
                    raise
                jitter = random.uniform(0, 0.5)  # nosec B311 - retry backoff jitter, not security-sensitive
                delay = self.retry_base_delay_seconds * (2**attempt) + jitter
                if time.monotonic() - start + delay > self.max_retry_seconds:
                    breaker.record_failure()
                    raise
                time.sleep(delay)
                attempt += 1
            else:
                breaker.record_success()
                return result

    def _generate_content(self, contents: str, config: types.GenerateContentConfig | None = None):
        """Tries self.model_name first, then each configured fallback model in order -- but only
        advances to the next model on a 429/404 (see _is_model_unavailable) or that model's own
        circuit breaker being open. Google's free-tier quota is per-model
        (GenerateRequestsPerDayPerProjectPerModel-FreeTier), so a model that's out of quota for
        the day doesn't affect a different model's quota; trying gemini-3.6-flash after
        gemini-3-flash-preview hits its daily cap effectively multiplies the day's total
        capacity. Any other failure (timeout, 5xx exhausted after retrying) propagates
        immediately instead of cascading through every fallback model -- that kind of failure
        isn't model-specific, so retrying it against a different model wouldn't help and would
        only multiply latency."""
        models = [self.model_name, *self.fallback_models]
        for i, model in enumerate(models):
            effective_config = config
            if effective_config is not None and model != self.model_name:
                # thinking_config is tuned specifically for self.model_name (see
                # query_generation_config below): confirmed in practice that a different model
                # (gemini-3.6-flash) 400s on thinking_budget=0, a value the primary model
                # accepts. A fallback attempt drops it and lets that model think however it
                # thinks by default, rather than fail outright over an incompatible tuning knob
                # that has nothing to do with why we're falling back in the first place.
                effective_config = effective_config.model_copy(update={"thinking_config": None})
            try:
                response = self._call_with_retry(
                    lambda m=model, c=effective_config: self.client.models.generate_content(
                        model=m, contents=contents, config=c
                    ),
                    breaker=self._breaker_for(model),
                )
            except Exception as exc:
                is_last = i == len(models) - 1
                if not is_last and (
                    _is_model_unavailable(exc) or isinstance(exc, CircuitBreakerOpenError)
                ):
                    continue
                raise
            self.last_model_used = model
            return response
        raise AssertionError("unreachable: the loop above always returns or raises")

    def _generate_json(self, prompt: str) -> dict:
        """Every app/agents/ chain (classifier, per-domain query agents) is built on this one
        call via LangChain prompt composition -- see query_generation_config above for why
        thinking is disabled for it."""
        response = self._generate_content(prompt, config=self.query_generation_config)
        return _extract_json(response.text)

    def generate_structured(self, prompt: str, schema: type[SchemaT]) -> SchemaT:
        return schema(**self._generate_json(prompt))

    def generate_structured_or_error(
        self, prompt: str, schema: type[SchemaT]
    ) -> SchemaT | QueryError:
        """Like generate_structured, but the prompt may instruct the model to respond with
        {"error": "<reason>"} instead of the target schema when the question is out of scope --
        the same fallback QuerySpec-generation always had, generalized to any schema."""
        data = self._generate_json(prompt)
        if "error" in data:
            return QueryError(**data)
        return schema(**data)

    def generate_answer(
        self,
        question: str,
        rows_by_domain: dict[str, list[dict]],
        skipped_domains: dict[str, str] | None = None,
    ) -> str:
        """skipped_domains (domain -> reason) is for domains that errored or declined out of
        scope -- passed as context, not appended after the fact, so the model can judge whether
        the gap actually matters instead of every skipped domain mechanically producing a "may
        be incomplete" caveat regardless of whether the other domains' data already answers the
        question in full (confirmed in production: this produced a misleading caveat on a fully
        correct, complete answer)."""
        prompt = _ANSWER_PROMPT.format(
            question=question,
            rows_by_domain=_rows_for_prompt(rows_by_domain, self.answer_max_rows),
            skipped_domains_note=(
                _SKIPPED_DOMAINS_NOTE.format(
                    skipped_domains=", ".join(
                        f"{domain} ({reason})" for domain, reason in skipped_domains.items()
                    )
                )
                if skipped_domains
                else ""
            ),
        )
        response = self._generate_content(prompt)
        return response.text.strip()
