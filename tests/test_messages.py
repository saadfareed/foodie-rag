"""The user-facing message catalogue.

Two things are worth asserting mechanically rather than by review: that no message leaks an
internal detail, and that every failure kind actually has text (a missing entry silently falls
back to "unknown", which is a worse answer than the one we meant to give).
"""

import pytest
from google.genai import errors as genai_errors
from pymongo.errors import ExecutionTimeout, ServerSelectionTimeoutError

from app.llm.circuit_breaker import CircuitBreakerOpenError
from app.messages import (
    Failure,
    classify_exception,
    failure_message,
    needs_reference,
    new_reference,
    no_data_message,
    out_of_scope_message,
    report_render_note,
    upload_failure_note,
)
from app.rag.validator import QueryValidationError


@pytest.mark.parametrize("failure", list(Failure))
def test_every_failure_kind_has_its_own_message(failure):
    """A missing entry falls back to UNKNOWN, which reads as "we have no idea" for a situation
    we actually understood."""
    text = failure_message(failure)

    assert text
    if failure is not Failure.UNKNOWN:
        assert text != failure_message(Failure.UNKNOWN)


@pytest.mark.parametrize("failure", list(Failure))
def test_no_message_names_an_internal(failure):
    """These strings go to a Slack channel. Naming a component teaches users vocabulary they
    can't act on, and naming a collection or field is an information leak."""
    text = failure_message(failure, "E-ABC123").lower()

    for internal in (
        "gemini",
        "mongo",
        "pymongo",
        "langgraph",
        "traceback",
        "exception",
        "queryspec",
        "collection",
        "usertype",
        "circuit breaker",
        "none",
        "null",
    ):
        assert internal not in text, f"{failure} leaks {internal!r}"


@pytest.mark.parametrize("failure", list(Failure))
def test_every_message_says_what_to_do_next(failure):
    """ "Something went wrong" with no next step leaves the user with nowhere to go."""
    text = failure_message(failure).lower()

    assert any(
        cue in text
        for cue in ("try", "wait", "ask again", "rephrase", "checking", "reference", "until")
    ), failure


def test_a_fault_carries_a_reference_code():
    text = failure_message(Failure.DATABASE_ERROR, "E-ABC123")

    assert "E-ABC123" in text


def test_an_expected_condition_carries_no_reference_code():
    """A rate limit isn't a fault. A code on it is noise that trains people to ignore codes."""
    assert not needs_reference(Failure.USER_RATE_LIMITED)
    assert "E-ABC123" not in failure_message(Failure.USER_RATE_LIMITED, "E-ABC123")


def test_references_are_short_and_collide_rarely():
    """Six hex characters: long enough not to collide within a log someone is actually reading,
    short enough to retype from a screenshot.

    Asserting *exact* uniqueness over 500 draws was flaky and had to be: 500 draws from a
    16.7M space collide about 0.75% of the time by the birthday bound, which is a test that fails
    roughly once every 130 runs for no reason at all. The property that actually matters is that
    collisions are rare enough to be irrelevant when grepping a day of logs, so that is what this
    asserts -- with a threshold far enough from the expected value (~1 collision at worst) to
    stay green, and close enough to catch a real regression like truncating to three characters.
    """
    references = [new_reference() for _ in range(500)]

    assert len(set(references)) >= 495
    assert all(r.startswith("E-") and len(r) == 8 for r in references)


@pytest.mark.parametrize(
    "exc,expected",
    [
        (CircuitBreakerOpenError("open"), Failure.UPSTREAM_UNAVAILABLE),
        (genai_errors.ClientError(429, {"error": {"code": 429}}), Failure.UPSTREAM_RATE_LIMITED),
        (genai_errors.ServerError(500, {"error": {"code": 500}}), Failure.UPSTREAM_UNAVAILABLE),
        (QueryValidationError("collection 'x' is not allowed"), Failure.QUERY_REJECTED),
        (ExecutionTimeout("timed out"), Failure.DATABASE_ERROR),
        (ServerSelectionTimeoutError("no server"), Failure.DATABASE_ERROR),
        (ValueError("something odd"), Failure.UNKNOWN),
        (KeyError("missing"), Failure.UNKNOWN),
    ],
)
def test_exceptions_map_to_the_right_failure(exc, expected):
    assert classify_exception(exc) is expected


# --- no data is not an error ----------------------------------------------------------------


@pytest.mark.parametrize("code", [400, 403, 404])
def test_a_client_error_from_the_model_api_is_not_called_transient(code):
    """A 4xx is a request *we* got wrong -- a malformed call, a retired model, a deadline the API
    refuses. "It usually recovers on its own within a few minutes" is advice for a wait that never
    ends: a real `400 INVALID_ARGUMENT: Manually set deadline 8s is too short` was reported to
    users that way, and nobody waiting could have fixed it. It needs a reference and an operator.
    """
    failure = classify_exception(genai_errors.ClientError(code, {"error": {"message": "boom"}}))

    assert failure is Failure.UNKNOWN
    assert needs_reference(failure)


def test_a_server_error_from_the_model_api_still_is_transient():
    """A 5xx genuinely is worth waiting out, and telling someone to fetch an admin for one would
    send them chasing an outage that fixes itself."""
    failure = classify_exception(genai_errors.ServerError(503, {"error": {"message": "busy"}}))

    assert failure is Failure.UPSTREAM_UNAVAILABLE


def test_rate_limiting_keeps_its_own_message():
    """429 is neither: it is expected, self-resolving, and gets no reference code."""
    failure = classify_exception(genai_errors.ClientError(429, {"error": {"message": "quota"}}))

    assert failure is Failure.UPSTREAM_RATE_LIMITED
    assert not needs_reference(failure)


def test_no_data_names_what_was_searched():
    """ "no data found" is a shrug; naming the domain makes the absence itself informative."""
    text = no_data_message(["orders"])

    assert "orders" in text
    assert "nothing matched" in text


def test_no_data_reads_as_an_answer_not_a_failure():
    """Telling someone "something went wrong" when the honest answer is "there are none" sends
    them looking for a bug that doesn't exist."""
    text = no_data_message(["orders"]).lower()

    assert "went wrong" not in text
    assert "error" not in text
    assert "reference" not in text


def test_no_data_lists_multiple_domains_readably():
    assert "customers, orders and vendors" in no_data_message(["orders", "vendors", "customers"])


def test_no_data_without_domains_still_advises():
    assert "widening" in no_data_message()


# --- model refusals -------------------------------------------------------------------------


def test_a_single_refusal_is_shown_verbatim():
    """The model's own wording is more specific than anything generic we could substitute."""
    assert out_of_scope_message({"orders": "I have no delivery-time field."}) == (
        "I have no delivery-time field."
    )


def test_several_refusals_are_attributed_to_their_domains():
    """Two unrelated sentences run together read like one confused thought."""
    text = out_of_scope_message({"orders": "no such field", "vendors": "no ratings history"})

    assert "orders: no such field" in text
    assert "vendors: no ratings history" in text


def test_no_refusals_falls_back_to_the_no_data_message():
    assert out_of_scope_message({}) == no_data_message()


# --- notes appended to a good answer --------------------------------------------------------


def test_the_render_note_does_not_claim_the_answer_failed():
    """It's appended below a correct answer; "something went wrong" would discard good work."""
    text = report_render_note("pdf")

    assert "PDF" in text
    assert "went wrong" not in text.lower()


def test_the_upload_note_names_a_missing_scope():
    text = upload_failure_note("pdf", "files:write")

    assert "files:write" in text
    assert "reinstall" in text.lower()


def test_the_upload_note_stays_generic_for_other_failures():
    text = upload_failure_note("csv", None)

    assert "CSV" in text
    assert "scope" not in text
