"""Every message this bot says that isn't a data answer, in one place.

Three things this fixes, all of which were real:

* **Raw internals leaked into Slack.** `f"...({exc})"` put pymongo tracebacks, validator text and
  Google's quota payload (metric names, doc links, a nested JSON blob) in front of users. That is
  both unreadable and an information leak -- an error message is an output channel like any
  other, and it was the only one not going through a policy.
* **The same situation was phrased differently depending on where it was caught.** A Gemini
  outage read one way when it escaped the graph and another way when a domain agent caught it.
* **Failures said what went wrong, not what to do.** "I ran into a problem" leaves the user with
  no next action; whether to retry, rephrase, or fetch an admin is the only thing they actually
  need from us.

**Reference codes.** Anything unexpected carries a short code (`E-4F2A9C`) that is also written
to the audit event. A user can quote it and an operator can `grep` for it -- which is the whole
difference between "the bot broke" and a diagnosable report. Expected outcomes (no data, a rate
limit, a clarification) deliberately get no code: they aren't faults, and a code on them would
train people to ignore codes.

House style, so a new message doesn't drift:
- Second person, plain, no apology theatre and no exclamation marks.
- Say what happened, then what to do next.
- Never name an internal component, exception type, collection or field.
"""

import uuid
from enum import Enum


class Failure(str, Enum):
    """Why an answer couldn't be produced. The value doubles as the audit-log `error` code."""

    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    UPSTREAM_RATE_LIMITED = "upstream_rate_limited"
    USER_RATE_LIMITED = "user_rate_limited"
    DAILY_BUDGET_EXCEEDED = "daily_budget_exceeded"
    DATABASE_ERROR = "database_error"
    QUERY_REJECTED = "query_rejected"
    GENERATION_ERROR = "generation_error"
    REPORT_RENDER_FAILED = "report_render_failed"
    UNKNOWN = "unknown_error"


#: Failures that are a normal, self-resolving condition rather than a fault. These get no
#: reference code -- there is nothing for an operator to look up, and the user already knows what
#: to do.
_EXPECTED = frozenset(
    {Failure.USER_RATE_LIMITED, Failure.DAILY_BUDGET_EXCEEDED, Failure.UPSTREAM_RATE_LIMITED}
)

_TEMPLATES: dict[Failure, str] = {
    Failure.UPSTREAM_UNAVAILABLE: (
        "I can't reach the service that interprets questions right now, so I can't answer this "
        "one. It usually recovers on its own within a few minutes -- try again shortly."
    ),
    Failure.UPSTREAM_RATE_LIMITED: (
        "I'm being rate-limited right now and can't process another question for a moment. "
        "Wait a minute and ask again."
    ),
    Failure.USER_RATE_LIMITED: (
        "You're asking faster than I can keep up. Give it a minute and ask again."
    ),
    Failure.DAILY_BUDGET_EXCEEDED: (
        "I've used up my question budget for today, so I can't run anything new until it "
        "resets tomorrow."
    ),
    Failure.DATABASE_ERROR: (
        "I understood your question but couldn't read the data to answer it. If this keeps "
        "happening, it's worth someone checking the database connection."
    ),
    Failure.QUERY_REJECTED: (
        "I understood your question, but the query I built for it wasn't one I'm allowed to "
        "run. Try asking for a narrower slice -- a shorter date range, or one specific status "
        "or vendor."
    ),
    Failure.GENERATION_ERROR: (
        "I couldn't turn that into a question I can run against the data. Try rephrasing it, "
        "or breaking it into two simpler questions."
    ),
    Failure.REPORT_RENDER_FAILED: (
        "I worked out the answer but couldn't build the file for it. The answer itself is "
        "above -- try asking for a different format, or a smaller slice of the data."
    ),
    Failure.UNKNOWN: (
        "Something went wrong while answering that, and it wasn't anything I recognised. "
        "Try again, and if it keeps happening pass the reference below to whoever runs this bot."
    ),
}


def new_reference() -> str:
    """A short, quotable code correlating a user's report with an audit-log entry.

    Six hex characters: long enough not to collide within a log someone is actually reading,
    short enough to retype from a screenshot without errors.
    """
    return f"E-{uuid.uuid4().hex[:6].upper()}"


def failure_message(failure: Failure, reference: str | None = None) -> str:
    """The user-facing text for `failure`, with the reference appended when there is one."""
    text = _TEMPLATES.get(failure, _TEMPLATES[Failure.UNKNOWN])
    if reference and failure not in _EXPECTED:
        return f"{text}\n\n_Reference: {reference}_"
    return text


def needs_reference(failure: Failure) -> bool:
    """Whether this failure warrants a reference code (i.e. it's a fault, not a normal state)."""
    return failure not in _EXPECTED


def no_data_message(domains: list[str] | None = None) -> str:
    """When the query ran fine and matched nothing.

    Deliberately distinct from every failure message: nothing went wrong, and telling someone
    "something went wrong" when the honest answer is "there are none" sends them to look for a
    bug that doesn't exist. Naming what was searched is what makes the difference visible --
    "no pending orders for that vendor" is an answer; "no data found" is a shrug.
    """
    if domains:
        searched = _join(sorted(domains))
        return (
            f"I checked {searched} and nothing matched that. The data may genuinely not have "
            "any, or a filter might be narrower than you meant -- try widening the date range "
            "or dropping one condition."
        )
    return (
        "Nothing in the data matched that. Try widening the date range or dropping one of the "
        "conditions."
    )


def report_render_note(output_format: str) -> str:
    """Appended to an otherwise-good answer when only the attachment failed.

    Deliberately a *note*, not a failure message: the answer above it is correct and complete,
    and replacing it with "something went wrong" would discard good work over a formatting
    problem the user can route around by asking for a different format.
    """
    return (
        f"(I answered above, but couldn't build the {output_format.upper()} file. "
        "Try a different format, or ask for a smaller slice of the data.)"
    )


def upload_failure_note(file_type: str, missing_scope: str | None = None) -> str:
    """When the file was built but Slack wouldn't accept it.

    A missing scope is named explicitly because it is a one-time install fix that will fail
    identically on every future report until someone makes it -- where a generic "couldn't
    upload" reads like a transient glitch worth retrying, which it never is.
    """
    label = file_type.upper()
    if missing_scope:
        return (
            f"(I built the {label} report but this app can't upload files: its Slack token is "
            f"missing the `{missing_scope}` scope. An admin can add it under OAuth & "
            "Permissions and reinstall the app.)"
        )
    return f"(I generated a {label} file but couldn't upload it to this channel.)"


def out_of_scope_message(reasons_by_domain: dict[str, str]) -> str:
    """When the model itself declined -- its wording is more specific than anything generic.

    A single reason is shown verbatim. Several are prefixed by domain, because otherwise two
    unrelated sentences run together into something that reads like one confused thought.
    """
    if not reasons_by_domain:
        return no_data_message()
    if len(reasons_by_domain) == 1:
        return next(iter(reasons_by_domain.values()))
    return "I couldn't answer parts of that:\n" + "\n".join(
        f"• {domain}: {reason}" for domain, reason in sorted(reasons_by_domain.items())
    )


def _join(items: list[str]) -> str:
    """ "orders", "orders and vendors", "orders, customers and vendors"."""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def classify_exception(exc: BaseException) -> Failure:
    """Map an exception to the failure the user should be told about.

    Imports are local to keep this module free of app dependencies -- it is the one place that
    every layer can import without risking a cycle.
    """
    from google.genai import errors as genai_errors
    from pymongo.errors import PyMongoError

    from app.llm.circuit_breaker import CircuitBreakerOpenError
    from app.rag.validator import QueryValidationError

    if isinstance(exc, CircuitBreakerOpenError):
        return Failure.UPSTREAM_UNAVAILABLE
    if isinstance(exc, genai_errors.APIError):
        if exc.code == 429:
            return Failure.UPSTREAM_RATE_LIMITED
        return Failure.UPSTREAM_UNAVAILABLE
    if isinstance(exc, QueryValidationError):
        return Failure.QUERY_REJECTED
    if isinstance(exc, PyMongoError):
        return Failure.DATABASE_ERROR
    return Failure.UNKNOWN
