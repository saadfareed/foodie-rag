"""Circuit breaker across all Gemini calls, shared by every worker and (optionally) every replica.

Retries (GeminiClient._call_with_retry) already handle *transient* single-call failures with
backoff, bounded by GEMINI_MAX_RETRY_SECONDS. What they don't handle is a *sustained* outage
(Gemini down, DNS/network partition): without this, every worker thread independently pays that
same full retry/timeout cost on every request for as long as the outage lasts, tying up the
entire pool instead of failing fast.

State lives in `app/state`, so with STATE_BACKEND=redis one replica discovering the outage spares
the others from rediscovering it. That is a genuine improvement rather than tidiness: the whole
value of a breaker is not paying the timeout twice, and per-replica breakers mean paying it once
per replica, every cooldown, for the entire outage.

Two keys, and the interaction between them is the whole state machine. `failures` counts
consecutive failures; `open` is a marker whose TTL *is* the cooldown, so the breaker closing again
is a key expiring rather than anything here checking a clock. GEMINI_CIRCUIT_BREAKER_THRESHOLD=0
disables the breaker entirely (every method below is then a no-op).

`name` namespaces those two keys, and it is load-bearing rather than cosmetic. GeminiClient keeps
one breaker *per model* (see `_breaker_for`), because a shared one meant one model exhausting its
daily quota tripped the breaker for the healthy fallback models too -- defeating the fallback
chain, confirmed in production. Sharing state across replicas must not quietly re-merge them, so
each breaker's name goes into its keys.
"""

from app.config import settings
from app.state import get_backend

_NAMESPACE = "circuit_breaker"
_FAILURES_KEY = "failures"
_OPEN_KEY = "open"
#: The failure counter is consecutive-failure state, not a rate window; it just needs to outlive a
#: cooldown so a half-open trial can see it, and not outlive an outage that has since resolved.
_FAILURES_TTL_SECONDS = 900.0


class CircuitBreakerOpenError(Exception):
    """Raised instead of even attempting a Gemini call while the breaker is open."""


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int,
        cooldown_seconds: float,
        name: str = "shared",
        backend=None,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._name = name
        self._backend = backend

    def _backend_or_default(self):
        return self._backend if self._backend is not None else get_backend()

    def _namespace(self) -> str:
        return f"{_NAMESPACE}:{self._name}"

    def before_call(self) -> None:
        """Raises CircuitBreakerOpenError if the breaker is open and still cooling down.

        Called before any real work (quota tracking, the actual HTTP call) so a fast-failed call
        doesn't also spend quota it never used.
        """
        if self._failure_threshold <= 0:
            return
        backend = self._backend_or_default()
        if backend.get(self._namespace(), _OPEN_KEY) is not None:
            failures = backend.peek(self._namespace(), _FAILURES_KEY)
            raise CircuitBreakerOpenError(
                f"Gemini circuit breaker open after {failures} consecutive failures -- "
                "failing fast instead of retrying."
            )
        # The open marker has expired, so the cooldown is over. Clearing the counter here is what
        # makes the next call a "half-open" trial: if it fails, record_failure() has to climb back
        # to the threshold before reopening, rather than reopening on a single failure forever.
        if backend.peek(self._namespace(), _FAILURES_KEY) >= self._failure_threshold:
            backend.delete(self._namespace(), _FAILURES_KEY)

    def record_success(self) -> None:
        if self._failure_threshold <= 0:
            return
        backend = self._backend_or_default()
        backend.delete(self._namespace(), _FAILURES_KEY)
        backend.delete(self._namespace(), _OPEN_KEY)

    def record_failure(self) -> None:
        if self._failure_threshold <= 0:
            return
        backend = self._backend_or_default()
        failures = backend.incr(self._namespace(), _FAILURES_KEY, ttl_seconds=_FAILURES_TTL_SECONDS)
        if failures >= self._failure_threshold:
            # Only written when not already open. Re-writing it on every further failure would
            # push the cooldown forward each time, so an outage that keeps producing errors would
            # never let a half-open trial call through and the breaker would never close.
            if backend.get(self._namespace(), _OPEN_KEY) is None:
                backend.set(
                    self._namespace(),
                    _OPEN_KEY,
                    b"1",
                    ttl_seconds=self._cooldown_seconds,
                    max_entries=0,
                )

    def failure_count(self) -> int:
        """Consecutive failures recorded since the last success or cooldown.

        Public because it is genuinely observable state -- how close the breaker is to opening --
        and because the alternative was tests reaching into a private attribute that no longer
        exists now that the count lives in a backend.
        """
        return self._backend_or_default().peek(self._namespace(), _FAILURES_KEY)

    def reset(self) -> None:
        """Close the breaker and forget the failure count. For tests and operator intervention."""
        backend = self._backend_or_default()
        backend.delete(self._namespace(), _FAILURES_KEY)
        backend.delete(self._namespace(), _OPEN_KEY)


gemini_circuit_breaker = CircuitBreaker(
    failure_threshold=settings.gemini_circuit_breaker_threshold,
    cooldown_seconds=settings.gemini_circuit_breaker_cooldown_seconds,
)
