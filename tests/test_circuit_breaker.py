import pytest

from app.llm.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError


def test_disabled_breaker_never_opens():
    breaker = CircuitBreaker(failure_threshold=0, cooldown_seconds=30)
    for _ in range(100):
        breaker.record_failure()
        breaker.before_call()  # must never raise


def test_breaker_opens_after_threshold_consecutive_failures():
    breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=30)
    breaker.before_call()  # ok
    breaker.record_failure()
    breaker.before_call()  # still ok (1 failure)
    breaker.record_failure()
    breaker.before_call()  # still ok (2 failures)
    breaker.record_failure()

    with pytest.raises(CircuitBreakerOpenError):
        breaker.before_call()


def test_success_resets_the_failure_count():
    breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=30)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()

    breaker.before_call()  # only 2 consecutive failures since the reset -- still closed


def test_breaker_half_opens_after_cooldown(monkeypatch):
    # The cooldown is a key TTL in the state backend now, not a timestamp this module holds --
    # "the breaker closes again" is that key expiring (see app/llm/circuit_breaker.py).
    import app.state.memory as state_memory

    now = [1000.0]
    monkeypatch.setattr(state_memory.time, "monotonic", lambda: now[0])

    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=10)
    breaker.record_failure()
    with pytest.raises(CircuitBreakerOpenError):
        breaker.before_call()

    now[0] += 11  # cooldown elapsed
    breaker.before_call()  # half-open: allowed through without raising


def test_a_failure_during_half_open_reopens_the_breaker(monkeypatch):
    # The cooldown is a key TTL in the state backend now, not a timestamp this module holds --
    # "the breaker closes again" is that key expiring (see app/llm/circuit_breaker.py).
    import app.state.memory as state_memory

    now = [1000.0]
    monkeypatch.setattr(state_memory.time, "monotonic", lambda: now[0])

    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=10)
    breaker.record_failure()
    now[0] += 11
    breaker.before_call()  # half-open trial
    breaker.record_failure()  # trial failed

    with pytest.raises(CircuitBreakerOpenError):
        breaker.before_call()
