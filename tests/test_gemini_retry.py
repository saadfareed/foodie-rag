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

    assert breaker._consecutive_failures == 1


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
    assert breaker._consecutive_failures == 0
