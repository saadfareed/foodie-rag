"""Entry point for answering a question: deterministic pre-checks -> the multi-domain LangGraph
agent (app/agents/graph.py) -> audit/answer-cache/quota integration -> the requested deliverable.

What stays here is everything that isn't specific to any one domain: the daily call budget gate,
the per-channel answer cache, the per-(channel,user) clarification cache, deterministic refusals,
structured audit logging, and turning the graph's rows into whatever format the user asked for.

**Ordering is the design.** Every gate that costs nothing runs before every gate that costs
something, and nothing reaches Gemini until all of them have passed:

    reset command / context-switch reply   -- pure string comparison
    deterministic refusal                  -- regex
    explicit format detection              -- regex
    answer cache                           -- dict lookup
    per-user rate limit                    -- deque trim
    shared daily Gemini budget             -- counter
    ... only now does anything call a model

That order is load-bearing rather than cosmetic. The budget check in particular used to sit
*after* an intent-classification Gemini call, so every request made while over budget spent a
real quota unit to report that the quota was exhausted.
"""

import logging
import time
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from app.agents.graph import build_graph
from app.audit.logger import log_query_event
from app.config import settings
from app.db.identity import fetch_contacts
from app.db.mongo import get_db
from app.generators.csv_generator import generate_csv
from app.generators.pdf_generator import generate_pdf
from app.generators.render_pool import RenderTimeout, run_render
from app.generators.xlsx_generator import generate_xlsx
from app.llm.gemini_client import GeminiClient
from app.llm.quota import quota_tracker
from app.messages import (
    Failure,
    classify_exception,
    failure_message,
    needs_reference,
    new_reference,
    report_render_note,
)
from app.rag.answer_cache import CachedResult, answer_cache
from app.rag.clarification_cache import PendingClarification, clarification_cache
from app.rag.context_switch_cache import PendingContextSwitch, context_switch_cache
from app.rag.conversation_context import conversation_context_cache
from app.rag.rate_limiter import daily_question_limiter, rate_limiter
from app.rag.stream import NULL_SINK, StreamSink
from app.security.output_scanner import scan_output_for_pii
from app.security.roles import ANONYMOUS, Principal, Role, may_see_contacts
from app.services.intent_router import detect_explicit_format, refusal_reason

logger = logging.getLogger("audit")


@dataclass(frozen=True)
class AnswerResult:
    """One answer, in whatever shape the user asked for.

    A single result type -- rather than "a str, except sometimes a dict when there's a file" --
    is what lets the answer cache round-trip a generated report intact. The earlier dual return
    type is why a cached PDF request replayed as bare prose with the attachment silently gone.
    """

    text: str
    file_bytes: bytes | None = None
    file_type: str | None = None
    #: Machine-readable reason this wasn't a normal answer (rate_limited, refused, ...).
    error: str | None = None

    @property
    def has_file(self) -> bool:
        return bool(self.file_bytes and self.file_type)


def _normalize_command(question: str) -> str:
    return question.strip().lower().rstrip("!.?")


# Exact-match phrases (after _normalize_command) that clear this user's conversational state
# instead of being treated as a question -- deterministic, no Gemini call, so it's checked before
# even the answer_cache and doesn't spend rate-limit/quota budget. Deliberately exact-match, not a
# substring/keyword search: a real question that happens to contain the word "reset" (e.g. "how
# many orders did we reset last week?") must still reach the classifier, not be swallowed by this.
_RESET_PHRASES = frozenset({"reset", "new topic", "start over", "forget that"})

# Replies to the "should I clear that context?" prompt (app/agents/graph.py's
# confirm_context_switch node) -- matched the same deterministic, exact-match way as the reset
# phrases above, so interpreting a yes/no never costs a Gemini call either.
_AFFIRMATIVE_PHRASES = frozenset(
    {"yes", "y", "yeah", "yep", "sure", "go ahead", "confirm", "correct"}
)
_NEGATIVE_PHRASES = frozenset({"no", "n", "nope", "cancel", "nevermind", "never mind"})


def _is_reset_command(question: str) -> bool:
    return _normalize_command(question) in _RESET_PHRASES


def _is_affirmative(question: str) -> bool:
    return _normalize_command(question) in _AFFIRMATIVE_PHRASES


def _is_negative(question: str) -> bool:
    return _normalize_command(question) in _NEGATIVE_PHRASES


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


def _with_contact_columns(
    rows_by_domain: dict[str, list[dict]], principal: Principal
) -> dict[str, list[dict]]:
    """Add contact columns to the rows going into a report, where this principal may see them.

    Runs **after** the answer, on the report path only, and never touches the rows the model was
    shown -- `app/security/field_policy.py` strips these fields on the way out of MongoDB, so the
    bot cannot be prompted into producing a contact list however the question is phrased. This
    adds a column, in code, to rows the principal was already authorized to see.

    Off unless REPORT_INCLUDE_CONTACTS is set: exposing contact details should be a deliberate
    operator decision, not something that arrives with an upgrade.
    """
    if not settings.report_include_contacts or principal.role is Role.ANONYMOUS:
        return rows_by_domain

    enriched = dict(rows_by_domain)
    for domain, rows in rows_by_domain.items():
        if not rows or not may_see_contacts(principal, domain):
            continue
        # Only `users`-shaped rows carry a user_id to look up. An orders table references people
        # by id, and those ids are resolved to *names* by app/agents/enrichment.py and then
        # dropped -- deliberately, so a report doesn't show USR-00031 beside Ayesha Khan. Adding
        # contacts there would mean re-introducing the id, which is a bigger change than this.
        ids = [row.get("user_id") for row in rows if isinstance(row, dict)]
        contacts = fetch_contacts(get_db(), [i for i in ids if i])
        if not contacts:
            continue
        enriched[domain] = [
            {**row, **contacts.get(row.get("user_id"), {})} if isinstance(row, dict) else row
            for row in rows
        ]
    return enriched


def _build_file(
    output_format: str,
    rows_by_domain: dict[str, list[dict]],
    question: str,
    answer: str,
    title: str,
    max_rows: int | None = None,
) -> tuple[bytes, str] | None:
    """Render the requested deliverable, or None if it can't be produced.

    Runs on the bounded render pool (app/generators/render_pool.py) so document rendering can't
    saturate every Socket Mode worker at once. A failure here is deliberately non-fatal: the text
    answer is already correct and complete, and handing it over beats failing the whole question
    because a chart didn't fit.
    """
    if output_format == "text":
        return None
    if not any(rows for rows in rows_by_domain.values()):
        return None

    builders = {
        "csv": lambda: generate_csv(rows_by_domain, title=title, max_rows=max_rows),
        "xlsx": lambda: generate_xlsx(rows_by_domain, title=title, max_rows=max_rows),
        "pdf": lambda: generate_pdf(
            rows_by_domain, question=question, answer=answer, title=title, max_rows=max_rows
        ),
    }
    builder = builders.get(output_format)
    if builder is None:
        return None

    try:
        return run_render(builder, description=f"{output_format} report"), output_format
    except RenderTimeout:
        return None
    except Exception:
        logger.exception("report_generation_failed", extra={"event": {"format": output_format}})
        return None


def answer_question(
    question: str,
    gemini: GeminiClient | None = None,
    *,
    user_id: str | None = None,
    channel_id: str | None = None,
    principal: Principal | None = None,
    progress: StreamSink | None = None,
) -> AnswerResult:
    """`principal` is who is asking, and therefore which rows the answer may be built from
    (app/security/roles.py). It defaults to ANONYMOUS -- which can read nothing -- rather than to
    an unrestricted identity: a caller that forgets to pass one gets a refusal, not everything.

    `progress` receives stage and token events as the answer is built (app/rag/stream.py).

    Optional, and a no-op sink by default, so the Slack adapter's behaviour is byte-identical:
    it has nowhere to put a progress event, since a Slack message appears only when finished.
    The events are a courtesy channel -- the returned AnswerResult is the same object either way,
    and a caller that ignores them loses nothing.
    """
    start = time.perf_counter()
    timings: dict[str, float] = {}
    sink = progress or NULL_SINK
    principal = principal or ANONYMOUS

    def _log(**kwargs) -> None:
        log_query_event(
            question=question,
            user_id=user_id,
            channel_id=channel_id,
            # Role and principal, not just the Slack/browser user id: when authorization decides
            # which rows an answer contains, the identity it was authorised under is the thing an
            # auditor needs, and it isn't recoverable from the question afterwards.
            **principal.audit_fields,
            duration_ms=(time.perf_counter() - start) * 1000,
            timings=timings,
            **kwargs,
        )

    def _plain(answer: str, *, error: str | None = None) -> AnswerResult:
        _log(error=error, answer=answer)
        return AnswerResult(text=answer, error=error)

    if _is_reset_command(question):
        # Ahead of answer_cache deliberately -- caching this reply would mean a second "reset"
        # from a different user in the same channel gets served the confirmation text without
        # actually clearing *their* clarification/context state (both caches are keyed by
        # (channel_id, user_id), not shared per-channel like answer_cache is).
        clarification_cache.clear(clarification_cache.make_key(channel_id, user_id))
        conversation_context_cache.clear(conversation_context_cache.make_key(channel_id, user_id))
        context_switch_cache.clear(context_switch_cache.make_key(channel_id, user_id))
        return _plain(
            "Got it -- I've cleared our conversation context. Ask me something new whenever "
            "you're ready.",
            error="context_reset",
        )

    # A pending confirmation means the *previous* answer asked "should I clear that context and
    # answer this as a new question?" (app/agents/graph.py's confirm_context_switch node) --
    # interpret this message as the reply, not a fresh question. Checked before answer_cache for
    # the same reason as the reset command above: deterministic, no Gemini call, must not be
    # cached, and must not miss a per-user reply because someone else's identical-text question
    # was cached first.
    switch_key = context_switch_cache.make_key(channel_id, user_id)
    pending_switch = context_switch_cache.get(switch_key)
    if pending_switch is not None:
        if _is_affirmative(question):
            # Confirmed -- drop the stale context and answer the original candidate question as
            # a clean, standalone question (previous_question stays unset below).
            context_switch_cache.clear(switch_key)
            conversation_context_cache.clear(
                conversation_context_cache.make_key(channel_id, user_id)
            )
            question = pending_switch.candidate_question
        elif _is_negative(question):
            context_switch_cache.clear(switch_key)
            return _plain(
                "Okay, sticking with our current conversation -- go ahead.",
                error="context_switch_declined",
            )
        else:
            # Neither a clear yes nor no -- the user moved on without answering, so whatever
            # context prompted the question is already stale. Drop both the pending prompt and
            # the old context (rather than just the prompt) so this message is judged purely on
            # its own merits instead of risking a second confirmation chained off context the
            # user never actually confirmed keeping.
            context_switch_cache.clear(switch_key)
            conversation_context_cache.clear(
                conversation_context_cache.make_key(channel_id, user_id)
            )

    # Policy refusal, decided in code (app/services/intent_router.py). Free, and ahead of the
    # cache because the answer never depends on the data -- there is nothing to look up and
    # nothing worth caching.
    refusal = refusal_reason(question)
    if refusal:
        return _plain(refusal, error="refused_restricted_request")

    # The format the user named outright. Resolved before the cache so the cache key can include
    # it; the classifier's inferred format (for questions that don't name one) is folded in
    # after the graph runs.
    explicit_format = detect_explicit_format(question)

    # Scoping the key by the principal is a correctness requirement, not a tuning knob: a
    # role-scoped answer contains only that identity's rows, so replaying it to a different asker
    # in the same channel would leak across tenants. Role *and* id -- two principals sharing a
    # user_id under different roles are answered from different rows.
    cache_key = answer_cache.make_key(
        channel_id,
        question,
        principal_scope=principal.cache_scope,
        output_format=explicit_format or "text",
    )
    cached = answer_cache.get(cache_key)
    if cached is not None:
        _log(error=cached.error, answer=cached.answer, cache_hit=True)
        return AnswerResult(
            text=cached.answer,
            file_bytes=cached.file_bytes,
            file_type=cached.file_type,
            error=cached.error,
        )

    # Checked before touching Gemini/Mongo/the shared daily budget at all -- a cache hit above
    # is free and shouldn't count against this user's own rate, but everything past this point
    # spends a real resource one user could otherwise monopolize.
    rate_limit_key = rate_limiter.make_key(channel_id, user_id)
    if not rate_limiter.allow(
        rate_limit_key, limit=settings.rate_limit_override_for(principal.role.value)
    ):
        return _plain(
            failure_message(Failure.USER_RATE_LIMITED),
            error=Failure.USER_RATE_LIMITED.value,
        )

    # The window above stops a burst; this stops a slow drain -- an identity asking a question
    # every thirty seconds all day passes every per-minute check and still exhausts a free-tier
    # quota by lunchtime. Keyed by principal rather than by channel, so it follows the person
    # rather than the place they happen to be asking from.
    if not daily_question_limiter.allow(principal.cache_scope):
        return _plain(
            failure_message(Failure.USER_DAILY_LIMIT),
            error=Failure.USER_DAILY_LIMIT.value,
        )

    # Ahead of every Gemini call, so an over-budget request costs nothing to refuse.
    if quota_tracker.is_over_budget():
        return _plain(
            failure_message(Failure.DAILY_BUDGET_EXCEEDED),
            error=Failure.DAILY_BUDGET_EXCEEDED.value,
        )

    gemini = gemini or GeminiClient()

    # A pending clarification for this (channel, user) means the *previous* answer was itself a
    # clarifying question -- treat this message as the follow-up, not a fresh question, by
    # feeding the classifier the combined text. This is why the cache is keyed by
    # (channel_id, user_id) rather than by question text: it's about one user's specific
    # back-and-forth (see app/rag/clarification_cache.py).
    clarification_key = clarification_cache.make_key(channel_id, user_id)
    pending = clarification_cache.get(clarification_key)
    effective_question = f"{pending.original_question} {question}".strip() if pending else question

    # Only consulted on a fresh (non-clarification) message -- a pending clarification already
    # has its own, more direct way of carrying context forward (the merge above), so checking
    # both would be redundant. This is the previous turn's *resolved* question, not raw chat
    # history (see app/rag/conversation_context.py); app/agents/classifier.py decides per-message
    # whether the current question actually needs it (Classification.context_mode), so an
    # unrelated question here costs nothing beyond this cache lookup.
    context_key = conversation_context_cache.make_key(channel_id, user_id)
    previous_question = None if pending else conversation_context_cache.get(context_key)

    graph = _get_graph(gemini)
    try:
        with _timed_stage(timings, "graph_ms"):
            result = graph.invoke(
                {
                    "question": effective_question,
                    "previous_question": previous_question,
                    "user_id": user_id,
                    "channel_id": channel_id,
                    "principal": principal,
                    "stream_sink": sink,
                }
            )
    except Exception as exc:
        # The raw exception goes to the audit log, never to Slack: it has carried pymongo
        # tracebacks and Google's quota payload (metric names, doc links, a nested JSON blob)
        # straight into a channel. The reference code is what ties the two together.
        failure = classify_exception(exc)
        reference = new_reference() if needs_reference(failure) else None
        _log(
            error=failure.value,
            error_detail=str(exc),
            error_reference=reference,
            answer=failure_message(failure, reference),
        )
        return AnswerResult(text=failure_message(failure, reference), error=failure.value)

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
        return _plain(answer, error="clarification_needed")

    if result.get("not_authorized"):
        # No query was generated and nothing was read. Returned before the cache is written, like
        # a clarification: caching a refusal under a channel-scoped key would replay it to the
        # next person who asks the same words, who may well be allowed to see the answer.
        return _plain(answer, error="not_authorized")

    if result.get("needs_context_confirmation"):
        # The classifier decided this message doesn't fit the still-live context but didn't
        # discard it -- park the candidate question until the reply comes in (see the
        # pending_switch handling above), instead of answering or touching
        # conversation_context_cache yet.
        context_switch_cache.set(
            switch_key, PendingContextSwitch(candidate_question=effective_question)
        )
        return _plain(answer, error="context_switch_confirmation_needed")

    clarification_cache.clear(clarification_key)
    # Stores resolved_question (the context-folded rewrite when this was itself a follow-up, or
    # just the question verbatim otherwise) -- never a growing transcript, so a chain of
    # follow-ups never costs more than one prior turn's text on the next classify call.
    conversation_context_cache.set(
        context_key, result.get("resolved_question") or effective_question
    )

    specs = list(result.get("specs_by_domain", {}).values())
    # Already sanitized -- app/db/executor.py applies the field policy as rows leave the
    # database, so nothing here (or in the answer prompt, or in a generated file) has ever seen
    # a raw `_id` or card number.
    rows_by_domain = result.get("rows_by_domain", {})
    row_count = sum(len(rows) for rows in rows_by_domain.values())
    errors_by_domain = {
        **(result.get("out_of_scope_by_domain") or {}),
        **(result.get("errors_by_domain") or {}),
    } or None

    # An explicitly named format wins over the classifier's inference: if the user typed "csv",
    # no model judgement should be able to hand them something else.
    classification = result.get("classification")
    output_format = explicit_format or getattr(classification, "output_format", "text")

    # Belt-and-braces over the field policy: the rows were sanitized on the way out of Mongo, but
    # the answer text is model-generated prose, and a model can restate a number it was shown.
    answer = scan_output_for_pii(answer)

    # The classifier's title restates the request ("Last 10 Incomplete Order Details"); the
    # configured REPORT_TITLE is the fallback when it had no opinion.
    report_title = (
        getattr(classification, "report_title", "") or ""
    ).strip() or settings.report_title

    file_result = None
    if output_format != "text":
        # Rendering a PDF is the most expensive CPU on the request path, and it happens *after*
        # the answer text is finished -- so without this the stream goes quiet for seconds
        # immediately after appearing to have finished.
        sink.stage("report", output_format)
        with _timed_stage(timings, "render_ms"):
            file_result = _build_file(
                output_format,
                _with_contact_columns(rows_by_domain, principal),
                effective_question,
                answer,
                report_title,
                max_rows=settings.report_max_rows_override_for(principal.role.value),
            )

    if output_format != "text" and file_result is None and row_count > 0:
        # Appended rather than replacing the answer: the prose is correct and complete, and the
        # only thing missing is the attachment. Saying "something went wrong" here would throw
        # away a good answer over a formatting problem.
        answer = f"{answer}\n\n_{report_render_note(output_format)}_"

    result_obj = AnswerResult(
        text=answer,
        file_bytes=file_result[0] if file_result else None,
        file_type=file_result[1] if file_result else None,
    )

    _log(specs=specs, row_count=row_count, errors_by_domain=errors_by_domain, answer=answer)
    # Stored under exactly the key that was looked up, so a repeat of this question replays the
    # same deliverable -- file included -- rather than re-rendering it. Rebuilding the key from
    # the *resolved* format instead would write to a key the next identical request never reads:
    # the lookup happens before the graph runs, so it can only ever know the explicitly named
    # format, and an inferred "pdf" would be filed under a key nothing looks for.
    answer_cache.set(
        cache_key,
        CachedResult(
            answer=answer,
            file_bytes=result_obj.file_bytes,
            file_type=result_obj.file_type,
        ),
    )
    return result_obj
