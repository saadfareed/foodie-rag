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
    USER_DAILY_LIMIT = "user_daily_limit"
    DAILY_BUDGET_EXCEEDED = "daily_budget_exceeded"
    DATABASE_ERROR = "database_error"
    QUERY_REJECTED = "query_rejected"
    GENERATION_ERROR = "generation_error"
    REPORT_RENDER_FAILED = "report_render_failed"
    NOT_AUTHENTICATED = "not_authenticated"
    SESSION_EXPIRED = "session_expired"
    UNKNOWN = "unknown_error"


#: Failures that are a normal, self-resolving condition rather than a fault. These get no
#: reference code -- there is nothing for an operator to look up, and the user already knows what
#: to do.
_EXPECTED = frozenset(
    {
        Failure.USER_RATE_LIMITED,
        Failure.USER_DAILY_LIMIT,
        Failure.DAILY_BUDGET_EXCEEDED,
        Failure.UPSTREAM_RATE_LIMITED,
        Failure.NOT_AUTHENTICATED,
        Failure.SESSION_EXPIRED,
    }
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
    Failure.USER_DAILY_LIMIT: (
        "You've reached your own question limit for today, so I can't run anything new until it "
        "resets at midnight UTC. If you need more, ask whoever administers this workspace."
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
    Failure.NOT_AUTHENTICATED: (
        "You need to be signed in before you can ask questions here. Sign in on this page "
        "and try again."
    ),
    Failure.SESSION_EXPIRED: (
        "Your chat session has expired. Reload the page to start a new one, then ask again."
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


def unknown_role_message() -> str:
    """A host application asked for a role that doesn't exist. Its developers read this, not a
    user, so it names the valid values -- an integration error nobody can act on is worse than
    one that says what to send."""
    return (
        "That role isn't one I recognise. Valid roles are admin, vendor and customer. "
        "Check the value being sent and try again."
    )


def role_not_permitted_message() -> str:
    """A host application asked for a role its key isn't allowed to assert."""
    return (
        "That role isn't enabled for this integration. An operator can allow it in the gateway "
        "configuration, then try again."
    )


def not_authorized_message(domains: list[str]) -> str:
    """When the question is fine but this person may not see that data.

    Names what was refused rather than saying "access denied": a customer asking about vendors
    needs to know it's the *vendor* half that isn't theirs, so they can ask the answerable half.
    Deliberately says nothing about whether the data exists -- "there are no such orders" and
    "those orders aren't yours" must read identically, or the refusal becomes a lookup tool.
    """
    subject = _join(sorted(domains))
    return (
        f"Your account doesn't have access to {subject} data, so I can't answer that. If you "
        "think it should, ask whoever administers this workspace."
    )


def sign_in_required_message() -> str:
    """When nobody is signed in at all. Distinct from a role refusal: the fix is different."""
    return (
        "I can only answer questions for a signed-in account, because what I can show you "
        "depends on who you are. Sign in and ask again."
    )


def invalid_credentials_message() -> str:
    """A sign-in that didn't work, said one way for every reason it didn't.

    Unknown address, wrong password and a suspended account are deliberately one sentence. Any
    difference between them turns the sign-in form into a way to test whether an address is
    registered -- the same reason `app/db/identity.py` returns a single `None` for all three.
    """
    return (
        "That email and password don't match an account that can sign in here. Check both and "
        "try again."
    )


def account_not_active_message() -> str:
    """When the password was right but the account is closed.

    The one sign-in outcome that is allowed to name its reason, because it is only ever reached
    *after* the password has verified -- the person has already proved the account is theirs, so
    this reveals nothing, and the alternative sends them to re-check a password that was correct.
    Says which door to knock on rather than just refusing.
    """
    return (
        "That account isn't active, so it can't be signed in to. Ask whoever administers this "
        "workspace to reactivate it, or use a different account."
    )


def account_creation_unavailable_message() -> str:
    """When a sign-up couldn't be written at all.

    Names the *situation* an operator can act on -- accounts can't be created against this
    database -- without naming the component, the driver or the error, like every other message
    here. In practice this is almost always a read-only database credential, which is the correct
    production posture rather than a fault: the reference code is how the two are told apart in
    the log.
    """
    return (
        "New accounts can't be created here right now. If this is your own environment, the "
        "database this connects to may not allow writes; otherwise ask whoever runs it."
    )


#: What each role can actually ask about, in the second person. This is the widget's opening
#: message, and it is per-role because the generic version was a small lie: a customer reading
#: "ask about your orders, customers or vendors" will ask about customers, and be refused by
#: `app/security/roles.py` for a reason the greeting implied wasn't there.
_ROLE_CAPABILITIES: dict[str, str] = {
    "admin": (
        "Ask about orders, customers or vendors across the whole workspace -- nothing you ask "
        "here is narrowed to one account."
    ),
    "vendor": (
        "Ask about your orders, the customers who have ordered from you, or your own vendor "
        "listing."
    ),
    "customer": "Ask about your own orders, or browse the vendor directory.",
}

_GENERIC_CAPABILITY = "Ask a question about your data."

_FORMATS_NOTE = "You can also ask for the answer as a CSV, Excel or PDF file."


def chat_welcome(role: str, name: str | None = None) -> str:
    """The widget's opening message: who we think you are, then what you can ask.

    Two lines rather than one, because they answer different questions. The greeting confirms
    which account the chat is answering as -- worth saying out loud when the answers are
    row-filtered by exactly that -- and the second line says what that account can reach.

    An unknown role falls back to the generic sentence rather than claiming access: over-promising
    here produces a refusal the user can't explain, and under-promising costs a question they can
    still ask anyway.
    """
    capability = _ROLE_CAPABILITIES.get(role.strip().lower(), _GENERIC_CAPABILITY)
    greeting = f"Hi {name.strip()}." if name and name.strip() else "Hi."
    return f"{greeting}\n\n{capability} {_FORMATS_NOTE}"


def too_many_related_records_message(subject: str) -> str:
    """When a scope had to be computed and came back too large to apply safely.

    Answering from a truncated set would be worse than refusing: the answer would look complete
    and quietly be about an arbitrary subset. Naming the way out is the whole message.
    """
    return (
        f"You have more {subject} than I can work through in one question. Try narrowing it -- "
        "a date range, or one status at a time."
    )


def question_required_message() -> str:
    """An empty message. Slack never delivers one; an HTTP client can post one all day."""
    return (
        'Ask me a question about your data and I\'ll look it up. Try "how many orders are pending?"'
    )


def question_too_long_message(max_chars: int) -> str:
    """A message past the length cap.

    Names the limit rather than just refusing: "too long" without a number leaves the user
    guessing how much to cut, which is the difference between one retry and several.
    """
    return (
        f"That message is longer than I can work with (the limit is {max_chars} characters). "
        "Try asking the shorter version of it."
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
    from app.llm.gemini_client import StreamInterrupted
    from app.rag.validator import QueryValidationError

    if isinstance(exc, CircuitBreakerOpenError):
        return Failure.UPSTREAM_UNAVAILABLE
    if isinstance(exc, StreamInterrupted):
        # The user has already read part of the answer. "Try again shortly" is still the right
        # advice, and the alternative -- the generic unknown-error text -- would imply the partial
        # answer above it came from something broken rather than something interrupted.
        return Failure.UPSTREAM_UNAVAILABLE
    if isinstance(exc, genai_errors.APIError):
        if exc.code == 429:
            return Failure.UPSTREAM_RATE_LIMITED
        if isinstance(exc.code, int) and 400 <= exc.code < 500:
            # A 4xx from the API is *us*, not an outage: a malformed request, a retired model, a
            # deadline the API won't accept. Telling the user it "usually recovers on its own
            # within a few minutes" is advice for a wait that never ends -- a real
            # `400 INVALID_ARGUMENT: Manually set deadline 8s is too short` was reported to users
            # that way, and nobody waiting could have fixed it. UNKNOWN carries a reference code
            # and says to pass it on, which is the only action that helps.
            return Failure.UNKNOWN
        return Failure.UPSTREAM_UNAVAILABLE
    if isinstance(exc, QueryValidationError):
        return Failure.QUERY_REJECTED
    if isinstance(exc, PyMongoError):
        return Failure.DATABASE_ERROR
    return Failure.UNKNOWN
