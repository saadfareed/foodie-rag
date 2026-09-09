import httpx
import pytest
from google.genai import errors

from app.llm.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError
from app.llm.gemini_client import GeminiClient, _is_retryable, is_rate_limited


def _client_error(code: int) -> errors.ClientError:
    return errors.ClientError(code, {"error": {"message": "boom"}})


def test_is_retryable_for_5xx():
    assert _is_retryable(_client_error(503)) is True
    assert _is_retryable(_client_error(500)) is True


def test_is_retryable_false_for_429():
    """429 is deliberately excluded from the retryable set: it's rate-limiting (often a per-day
    quota on the free tier), not a transient server fault, so retrying with our few-second
    backoff schedule can't succeed before the quota resets -- it would just add latency for a
    guaranteed second failure. See is_rate_limited() for the user-facing message instead."""
    assert _is_retryable(_client_error(429)) is False


def test_is_retryable_false_for_4xx_non_429():
    assert _is_retryable(_client_error(400)) is False
    assert _is_retryable(ValueError("not an api error")) is False


def test_is_retryable_true_for_client_side_timeouts():
    """A hung/slow request past our own http_options timeout raises httpx.TimeoutException, not
    a google.genai.errors.APIError -- this must still be retried, otherwise a single slow (but
    transient) response fails the whole question with zero retry attempts, as happened in
    production: a 15s client timeout on query generation raised httpx.ReadTimeout and was treated
    as non-retryable, so the request failed outright instead of getting a second chance."""
    assert _is_retryable(httpx.ReadTimeout("The read operation timed out")) is True
    assert _is_retryable(httpx.ConnectTimeout("connect timed out")) is True


def test_is_rate_limited_true_only_for_429():
    assert is_rate_limited(_client_error(429)) is True
    assert is_rate_limited(_client_error(503)) is False
    assert is_rate_limited(ValueError("not an api error")) is False


def _make_client(max_retries: int, retry_base_delay_seconds: float, max_retry_seconds: float = 60):
    client = GeminiClient.__new__(GeminiClient)
    client.max_retries = max_retries
    client.retry_base_delay_seconds = retry_base_delay_seconds
    client.max_retry_seconds = max_retry_seconds
    return client


def test_call_with_retry_succeeds_without_retry(monkeypatch):
    monkeypatch.setattr("app.llm.gemini_client.time.sleep", lambda _: None)
    client = _make_client(max_retries=3, retry_base_delay_seconds=0)

    result = client._call_with_retry(lambda: "ok")

    assert result == "ok"


def test_call_with_retry_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr("app.llm.gemini_client.time.sleep", lambda _: None)
    client = _make_client(max_retries=3, retry_base_delay_seconds=0)

    calls = {"count": 0}

    def flaky():
        calls["count"] += 1
        if calls["count"] < 3:
            raise _client_error(503)
        return "recovered"

    result = client._call_with_retry(flaky)

    assert result == "recovered"
    assert calls["count"] == 3


def test_call_with_retry_retries_on_client_side_timeout(monkeypatch):
    monkeypatch.setattr("app.llm.gemini_client.time.sleep", lambda _: None)
    client = _make_client(max_retries=3, retry_base_delay_seconds=0)

    calls = {"count": 0}

    def times_out_then_succeeds():
        calls["count"] += 1
        if calls["count"] < 2:
            raise httpx.ReadTimeout("The read operation timed out")
        return "recovered"

    assert client._call_with_retry(times_out_then_succeeds) == "recovered"
    assert calls["count"] == 2


def test_call_with_retry_raises_immediately_on_non_retryable(monkeypatch):
    monkeypatch.setattr("app.llm.gemini_client.time.sleep", lambda _: None)
    client = _make_client(max_retries=3, retry_base_delay_seconds=0)

    calls = {"count": 0}

    def always_bad():
        calls["count"] += 1
        raise _client_error(400)

    with pytest.raises(errors.ClientError):
        client._call_with_retry(always_bad)

    assert calls["count"] == 1


def test_call_with_retry_raises_immediately_on_429_without_retrying(monkeypatch):
    """Regression test for wasted latency in production: a 429 from a per-day free-tier quota
    used to be retried up to max_retries times (each attempt immediately failing the same way)
    before giving up, adding several seconds of latency for a request that could never succeed
    within the retry window."""
    monkeypatch.setattr("app.llm.gemini_client.time.sleep", lambda _: None)
    client = _make_client(max_retries=3, retry_base_delay_seconds=0)

    calls = {"count": 0}

    def always_429():
        calls["count"] += 1
        raise _client_error(429)

    with pytest.raises(errors.ClientError):
        client._call_with_retry(always_429)

    assert calls["count"] == 1


def test_call_with_retry_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr("app.llm.gemini_client.time.sleep", lambda _: None)
    client = _make_client(max_retries=2, retry_base_delay_seconds=0)

    calls = {"count": 0}

    def always_503():
        calls["count"] += 1
        raise _client_error(503)

    with pytest.raises(errors.ClientError):
        client._call_with_retry(always_503)

    assert calls["count"] == 3  # initial attempt + 2 retries


def test_call_with_retry_stops_early_once_deadline_would_be_exceeded(monkeypatch):
    """Even with retries remaining, a request must not stall past max_retry_seconds."""
    monkeypatch.setattr("app.llm.gemini_client.time.sleep", lambda _: None)
    monkeypatch.setattr("app.llm.gemini_client.random.uniform", lambda _a, _b: 0)
    # base_delay=10 means the very first retry's delay (10s) alone exceeds the 1s deadline.
    client = _make_client(max_retries=5, retry_base_delay_seconds=10, max_retry_seconds=1)

    calls = {"count": 0}

    def always_503():
        calls["count"] += 1
        raise _client_error(503)

    with pytest.raises(errors.ClientError):
        client._call_with_retry(always_503)

    # Gives up after the first failed attempt instead of sleeping past the deadline.
    assert calls["count"] == 1


def test_call_with_retry_allows_retries_within_deadline(monkeypatch):
    monkeypatch.setattr("app.llm.gemini_client.time.sleep", lambda _: None)
    monkeypatch.setattr("app.llm.gemini_client.random.uniform", lambda _a, _b: 0)
    client = _make_client(max_retries=5, retry_base_delay_seconds=0, max_retry_seconds=60)

    calls = {"count": 0}

    def flaky():
        calls["count"] += 1
        if calls["count"] < 2:
            raise _client_error(503)
        return "ok"

    assert client._call_with_retry(flaky) == "ok"
    assert calls["count"] == 2


def test_call_with_retry_records_a_single_failure_per_call_not_per_internal_attempt(monkeypatch):
    """A call that fails after exhausting its own internal retries should count as exactly one
    circuit-breaker failure -- otherwise routine retried-then-still-fails calls would trip the
    breaker much faster than "N consecutive calls failed" implies."""
    monkeypatch.setattr("app.llm.gemini_client.time.sleep", lambda _: None)
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=30)
    monkeypatch.setattr("app.llm.gemini_client.gemini_circuit_breaker", breaker)
    client = _make_client(max_retries=3, retry_base_delay_seconds=0)

    def always_503():
        raise errors.ClientError(503, {"error": {"message": "boom"}})

    with pytest.raises(errors.ClientError):
        client._call_with_retry(always_503)

    assert breaker.failure_count() == 1


def test_call_with_retry_raises_circuit_breaker_open_without_calling_fn(monkeypatch):
    monkeypatch.setattr("app.llm.gemini_client.time.sleep", lambda _: None)
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=30)
    breaker.record_failure()  # pre-open the breaker
    monkeypatch.setattr("app.llm.gemini_client.gemini_circuit_breaker", breaker)
    client = _make_client(max_retries=3, retry_base_delay_seconds=0)

    calls = {"count": 0}

    def should_not_be_called():
        calls["count"] += 1
        return "ok"

    with pytest.raises(CircuitBreakerOpenError):
        client._call_with_retry(should_not_be_called)

    assert calls["count"] == 0


def test_call_with_retry_records_success_and_clears_prior_failures(monkeypatch):
    monkeypatch.setattr("app.llm.gemini_client.time.sleep", lambda _: None)
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=30)
    monkeypatch.setattr("app.llm.gemini_client.gemini_circuit_breaker", breaker)
    client = _make_client(max_retries=0, retry_base_delay_seconds=0)

    assert client._call_with_retry(lambda: "ok") == "ok"
    assert breaker.failure_count() == 0


# --- slow failures ------------------------------------------------------------------------------
#
# Every test above makes its failures instantaneous, which is what let a real bug through: the
# retry budget is measured from the start of the *first attempt*, so it is spent by the attempt
# itself, and the failures most worth retrying (a 504, our own timeout) are exactly the slow ones.
# With a 15s request timeout and an 8s budget, a production 504 arriving at 12.1s was refused a
# retry it was classified as deserving. These tests give the failure a duration.


class _Clock:
    """A monotonic clock the test drives. `attempt_seconds` is how long each failure takes --
    in production that is bounded by GEMINI_REQUEST_TIMEOUT_MS."""

    def __init__(self, attempt_seconds: float = 8.0):
        self.t = 1000.0
        self.attempt_seconds = attempt_seconds
        self.slept = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds

    def spend_an_attempt(self) -> None:
        self.t += self.attempt_seconds


def _with_clock(monkeypatch, clock: _Clock) -> None:
    monkeypatch.setattr("app.llm.gemini_client.time.monotonic", clock.now)
    monkeypatch.setattr("app.llm.gemini_client.time.sleep", clock.sleep)
    monkeypatch.setattr("app.llm.gemini_client.random.uniform", lambda _a, _b: 0)


def test_a_slow_failure_is_still_retried(monkeypatch):
    """The regression. An 8s failure inside a 16s budget leaves room for a second attempt; the
    shipped defaults are chosen so this holds."""
    clock = _Clock(attempt_seconds=8.0)
    _with_clock(monkeypatch, clock)
    client = _make_client(max_retries=3, retry_base_delay_seconds=1, max_retry_seconds=16)

    calls = {"count": 0}

    def slow_504():
        calls["count"] += 1
        clock.spend_an_attempt()
        if calls["count"] < 2:
            raise _client_error(504)
        return "recovered"

    assert client._call_with_retry(slow_504) == "recovered"
    assert calls["count"] == 2


def test_a_budget_below_the_attempt_timeout_retries_nothing(monkeypatch):
    """The bug, pinned as behaviour so the two settings can't drift back into disagreeing
    silently. settings.retry_budget_warning() is what says this out loud at startup."""
    clock = _Clock(attempt_seconds=12.1)  # the 504 in the production audit log
    _with_clock(monkeypatch, clock)
    client = _make_client(max_retries=3, retry_base_delay_seconds=1, max_retry_seconds=8)

    calls = {"count": 0}

    def slow_504():
        calls["count"] += 1
        clock.spend_an_attempt()
        raise _client_error(504)

    with pytest.raises(errors.APIError):
        client._call_with_retry(slow_504)

    assert calls["count"] == 1, "the first attempt spends the whole budget before a retry is due"


def test_a_slow_client_side_timeout_is_retried_too(monkeypatch):
    """_is_retryable was extended to cover httpx timeouts precisely so this happens. It never did
    with the shipped settings, because a timeout takes the full request timeout to raise."""
    clock = _Clock(attempt_seconds=8.0)
    _with_clock(monkeypatch, clock)
    client = _make_client(max_retries=3, retry_base_delay_seconds=1, max_retry_seconds=16)

    calls = {"count": 0}

    def slow_timeout():
        calls["count"] += 1
        clock.spend_an_attempt()
        if calls["count"] < 2:
            raise httpx.ReadTimeout("The read operation timed out")
        return "recovered"

    assert client._call_with_retry(slow_timeout) == "recovered"
    assert calls["count"] == 2


def test_the_retry_budget_is_shared_across_fallback_models(monkeypatch):
    """One budget for the whole sweep, so *retrying* isn't multiplied by the number of models
    configured -- but a model that never gets one attempt is not a fallback, so past the deadline
    each still gets exactly one. The honest worst case is therefore the budget plus one attempt
    per fallback, which is why GEMINI_FALLBACK_MODELS is a list an operator sizes deliberately."""
    clock = _Clock(attempt_seconds=8.0)
    _with_clock(monkeypatch, clock)
    client = _make_client(max_retries=3, retry_base_delay_seconds=1, max_retry_seconds=16)
    client.model_name = "primary"
    client.fallback_models = ["second", "third"]
    client._breakers = {}

    attempts = []

    def attempt(model):
        attempts.append(model)
        clock.spend_an_attempt()
        raise _client_error(504)

    with pytest.raises(errors.APIError):
        client._for_each_model(attempt)

    # primary retries once inside the budget; by then it is spent, so the other two get one each.
    assert attempts == ["primary", "primary", "second", "third"]
    # The budget bounds when the last attempt may *start*; every attempt still runs for as long
    # as the request timeout allows. So the sweep's true bound is the budget plus one attempt for
    # each model that can still be entered -- worth stating as arithmetic, because reading the
    # ceiling as "the most this can take" is the exact mistake that produced the original bug.
    budget, attempt_seconds, models = 16, 8, 3
    assert clock.t - 1000.0 <= budget + attempt_seconds * models
    assert clock.slept == [1], "one backoff for the whole sweep, not one per model"
