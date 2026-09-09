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
from app.security.output_scanner import StreamingRedactor

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

Style: get straight to the answer, in 1-3 short sentences. Lead with the number or finding the
question asked for, then at most one sentence of context (the largest contributor, a notable
trend). Do not include meta-commentary about what the data doesn't contain.

Do NOT reproduce the rows as a table or a list. Every row is already delivered to the user
separately -- as an on-screen summary, or as a CSV/Excel/PDF attachment built from this same
data. Restating them here duplicates the attachment, and truncating them mid-way (only some of
the rows are shown below) would contradict it. Summarize; never transcribe.

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


class StreamInterrupted(Exception):
    """A streaming call failed *after* text had already reached the user.

    Deliberately not retryable and deliberately not a reason to try a fallback model: both would
    restart the answer from the beginning, and the user has already read the first half of the
    previous attempt. The only honest outcome is to stop and say so.
    """


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, errors.APIError):
        return exc.code in _RETRYABLE_STATUS_CODES
    # A client-side read/connect timeout is transient by nature -- worth a retry rather than an
    # immediate hard failure. Without this, a single slow response fails the whole question with
    # no retry at all (this is what happened in production: a 15s HTTP timeout on the
    # query-generation call raised httpx.ReadTimeout, which wasn't a google.genai.errors.APIError,
    # so _call_with_retry gave up on the first attempt instead of retrying).
    return isinstance(exc, httpx.TimeoutException)


def _is_deadline_exceeded(exc: Exception) -> bool:
    """True for a failure that is *this attempt taking too long*, from either end of the wire.

    Google's `504 DEADLINE_EXCEEDED` and our own client-side timeout are the same event seen from
    two sides, and both mean the request was accepted and then not answered in time -- unlike a
    500/502/503, which is a server that failed rather than stalled. That distinction is what makes
    this worth advancing a fallback model for: a different model is a different queue, and the
    thing that just ran out of time was the queue.
    """
    if isinstance(exc, errors.APIError):
        return exc.code == 504
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


def _prune_value(value: object, max_chars: int) -> object:
    """Shorten one field value so a single huge blob can't dominate the prompt.

    Nested documents and arrays are summarized rather than serialized in full: a GeoJSON
    polygon or an embedded audit trail contributes thousands of tokens and nothing the answer
    needs, since the question was about the row, not its internals.
    """
    if isinstance(value, str) and len(value) > max_chars:
        return value[:max_chars] + "…"
    if isinstance(value, list):
        if len(value) > 5:
            return [_prune_value(v, max_chars) for v in value[:5]] + [f"…{len(value) - 5} more"]
        return [_prune_value(v, max_chars) for v in value]
    if isinstance(value, dict):
        return {k: _prune_value(v, max_chars) for k, v in list(value.items())[:8]}
    return value


def _rows_for_prompt(
    rows_by_domain: dict[str, list[dict]], max_rows: int, max_field_chars: int = 200
) -> str:
    """Cap the rows serialized into the answer prompt so payload size (and Gemini latency) stays
    bounded regardless of how many rows the query returned -- the cap applies per domain, so one
    chatty domain can't crowd the others out of the prompt entirely.

    The row cap alone bounded the row *count* but not the row *width*: 30 documents with a long
    description or an embedded array each are still an enormous prompt, on the single largest
    call in the request. `_prune_value` bounds each field as well, so the prompt is bounded in
    both dimensions.
    """
    capped: dict[str, list] = {}
    for domain, rows in rows_by_domain.items():
        visible = rows[:max_rows]
        pruned: list = [
            {k: _prune_value(v, max_field_chars) for k, v in row.items()}
            if isinstance(row, dict)
            else row
            for row in visible
        ]
        if len(rows) > max_rows:
            pruned.append(f"...{len(rows) - max_rows} more row(s) omitted for brevity...")
        capped[domain] = pruned
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
                # Namespaces this breaker's state (app/llm/circuit_breaker.py) -- without it, a
                # shared backend would put every model's failures back in one bucket and undo the
                # per-model isolation this dict exists to provide.
                name=model,
            )
            self._breakers[model] = breaker
        return breaker

    def _call_with_retry(
        self,
        fn: Callable[[], R],
        breaker: CircuitBreaker | None = None,
        deadline: float | None = None,
    ) -> R:
        """Call `fn`, retrying transient failures until the retry budget runs out.

        `deadline` is a `time.monotonic()` value shared across every model in one
        `_for_each_model` sweep, so falling through to a fallback model cannot multiply the budget
        by the number of models configured. Past the deadline this still makes **one** attempt --
        the budget bounds retrying, and a model that never gets a single try is not a fallback.
        """
        # Defaults to the shared module-level breaker only when no per-model breaker is given --
        # every real call site (_generate_content) always passes one explicitly (see
        # _breaker_for). Checked first, before quota_tracker.record_call(): a call the breaker
        # fast-fails never actually reaches Gemini, so it shouldn't be charged against the daily
        # budget either.
        breaker = gemini_circuit_breaker if breaker is None else breaker
        breaker.before_call()
        quota_tracker.record_call()
        attempt = 0
        if deadline is None:
            deadline = time.monotonic() + self.max_retry_seconds
        while True:
            try:
                result = fn()
            except Exception as exc:
                if attempt >= self.max_retries or not _is_retryable(exc):
                    breaker.record_failure()
                    raise
                jitter = random.uniform(0, 0.5)  # nosec B311 - retry backoff jitter, not security-sensitive
                delay = self.retry_base_delay_seconds * (2**attempt) + jitter
                # The budget covers the attempts as well as the sleeps, which is why
                # settings.retry_budget_warning() exists: a ceiling below the per-attempt timeout
                # means a slow failure has already spent it here, and the retry this exception was
                # classified as deserving never happens.
                if time.monotonic() + delay > deadline:
                    breaker.record_failure()
                    raise
                time.sleep(delay)
                attempt += 1
            else:
                breaker.record_success()
                return result

    def _for_each_model(self, attempt: Callable[[str], R]) -> R:
        """Run `attempt` against the primary model, then each fallback, and return its result.

        Three conditions advance to the next model, and each is a statement that *this model*
        can't serve the request right now:

        * **429/404** (see _is_model_unavailable). Google's free-tier quota is per-model
          (GenerateRequestsPerDayPerProjectPerModel-FreeTier), so a model that's out of quota for
          the day doesn't affect a different model's quota; trying gemini-3.6-flash after
          gemini-3-flash-preview hits its daily cap effectively multiplies the day's capacity.
        * **That model's circuit breaker being open** -- it is per-model for this reason.
        * **A deadline exceeded** (see _is_deadline_exceeded), but only after this model's own
          retries are spent. A 504 is a request that was accepted and then not answered in time,
          which in practice is a busy model rather than a broken network -- a real one arrived
          12.1s into query generation on a preview model that had answered a classify call two
          seconds earlier. A different model is a different queue. A plain 500/502/503 still
          propagates: that is a server that failed rather than stalled, and nothing about it says
          the next model would do better.

        Every model shares **one** retry budget (`deadline` below), so *retrying* is not multiplied
        by the number of models configured: whichever model is running when the budget runs out is
        the last one to get a second try. Each subsequent model still gets its one attempt -- a
        model that never runs isn't a fallback -- so the honest worst case for a sweep is the
        budget plus one attempt per model -- the budget bounds when an attempt may *start*, never
        how long it then runs. That is why GEMINI_FALLBACK_MODELS is a list an operator sizes
        deliberately rather than an unbounded chain.

        Shared by _generate_content and stream_answer so the fallback policy is stated once. A
        streaming call that already delivered text raises StreamInterrupted, which is none of the
        three advance conditions -- so it stops here rather than restarting the answer from the
        beginning under a different model, however it failed.
        """
        models = [self.model_name, *self.fallback_models]
        deadline = time.monotonic() + self.max_retry_seconds
        for i, model in enumerate(models):
            try:
                result = self._call_with_retry(
                    lambda m=model: attempt(m),
                    breaker=self._breaker_for(model),
                    deadline=deadline,
                )
            except Exception as exc:
                is_last = i == len(models) - 1
                if not is_last and (
                    _is_model_unavailable(exc)
                    or _is_deadline_exceeded(exc)
                    or isinstance(exc, CircuitBreakerOpenError)
                ):
                    continue
                raise
            self.last_model_used = model
            return result
        raise AssertionError("unreachable: the loop above always returns or raises")

    def _generate_content(self, contents: str, config: types.GenerateContentConfig | None = None):
        def attempt(model: str):
            effective_config = config
            if effective_config is not None and model != self.model_name:
                # thinking_config is tuned specifically for self.model_name (see
                # query_generation_config below): confirmed in practice that a different model
                # (gemini-3.6-flash) 400s on thinking_budget=0, a value the primary model
                # accepts. A fallback attempt drops it and lets that model think however it
                # thinks by default, rather than fail outright over an incompatible tuning knob
                # that has nothing to do with why we're falling back in the first place.
                effective_config = effective_config.model_copy(update={"thinking_config": None})
            return self.client.models.generate_content(
                model=model, contents=contents, config=effective_config
            )

        return self._for_each_model(attempt)

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
        response = self._generate_content(
            self._answer_prompt(question, rows_by_domain, skipped_domains)
        )
        return response.text.strip()

    def _answer_prompt(
        self,
        question: str,
        rows_by_domain: dict[str, list[dict]],
        skipped_domains: dict[str, str] | None,
    ) -> str:
        """Shared by generate_answer and stream_answer, so the streamed answer and the blocking
        one are the same answer -- two prompt builders would drift, and the difference would only
        show up as "the Slack reply and the web reply disagree"."""
        return _ANSWER_PROMPT.format(
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

    def stream_answer(
        self,
        question: str,
        rows_by_domain: dict[str, list[dict]],
        skipped_domains: dict[str, str] | None = None,
        *,
        on_text: Callable[[str], None],
    ) -> str:
        """Same answer as generate_answer, delivered as it is written.

        `on_text` receives text that has already been through the PII redactor -- streaming
        straight from the model would bypass `scan_output_for_pii`, which runs on the *finished*
        prose and so would arrive long after a card number had been displayed. See
        `app/security/output_scanner.py::StreamingRedactor` for why chunk-at-a-time redaction is
        not simply "scan each chunk".

        The return value is the complete raw answer, exactly as `generate_answer` returns it, so
        every downstream step -- the output scan, the report builder, the answer cache -- behaves
        identically whether or not anyone was watching it arrive.
        """
        prompt = self._answer_prompt(question, rows_by_domain, skipped_domains)
        redactor = StreamingRedactor()
        collected: list[str] = []

        def consume(model: str) -> None:
            delivered = False
            try:
                for chunk in self.client.models.generate_content_stream(
                    model=model, contents=prompt
                ):
                    text = getattr(chunk, "text", None)
                    if not text:
                        continue
                    collected.append(text)
                    safe = redactor.feed(text)
                    if safe:
                        delivered = True
                        on_text(safe)
            except Exception as exc:
                if delivered or collected:
                    raise StreamInterrupted(str(exc)) from exc
                raise

        self._for_each_model(consume)
        tail = redactor.finish()
        if tail:
            on_text(tail)
        return "".join(collected).strip()
