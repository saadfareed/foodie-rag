"""Circuit breaker across all Gemini calls, shared process-wide.

Retries (GeminiClient._call_with_retry) already handle *transient* single-call failures with
backoff, bounded by GEMINI_MAX_RETRY_SECONDS. What they don't handle is a *sustained* outage
(Gemini down, DNS/network partition): without this, every one of the Socket Mode worker threads
(app/config.py::slack_socket_mode_concurrency) independently pays that same full retry/timeout
cost on every request for as long as the outage lasts, tying up the entire thread pool instead of
failing fast.

Deliberately module-level (like app/llm/quota.py::quota_tracker), not a per-GeminiClient-instance
attribute: production shares exactly one GeminiClient (see app/main.py), and the failure signal
here is about the shared Gemini backend being unreachable, not about any particular client
object. Tracks *consecutive* failures across calls; once GEMINI_CIRCUIT_BREAKER_THRESHOLD is hit,
the breaker "opens" and every call fails immediately with CircuitBreakerOpenError for
GEMINI_CIRCUIT_BREAKER_COOLDOWN_SECONDS, instead of even attempting the request. GEMINI_CIRCUIT_
BREAKER_THRESHOLD=0 disables the breaker entirely (every method below is then a no-op).
"""

import threading
import time

from app.config import settings


class CircuitBreakerOpenError(Exception):
    """Raised instead of even attempting a Gemini call while the breaker is open."""


class CircuitBreaker:
    def __init__(self, failure_threshold: int, cooldown_seconds: float) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    def before_call(self) -> None:
        """Raises CircuitBreakerOpenError if the breaker is open and still cooling down. Called
        before any real work (quota tracking, the actual HTTP call) so a fast-failed call doesn't
        also spend quota it never used."""
        if self._failure_threshold <= 0:
            return
        with self._lock:
            if self._opened_at is None:
                return
            if time.monotonic() - self._opened_at < self._cooldown_seconds:
                raise CircuitBreakerOpenError(
                    f"Gemini circuit breaker open after {self._consecutive_failures} "
                    "consecutive failures -- failing fast instead of retrying."
                )
            # Cooldown elapsed: let exactly one trial call through ("half-open"). If it fails,
            # record_failure() re-opens the breaker; if it succeeds, record_success() clears it.
            self._opened_at = None
            self._consecutive_failures = 0

    def record_success(self) -> None:
        if self._failure_threshold <= 0:
            return
        with self._lock:
            self._consecutive_failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        if self._failure_threshold <= 0:
            return
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._failure_threshold and self._opened_at is None:
                self._opened_at = time.monotonic()


gemini_circuit_breaker = CircuitBreaker(
    failure_threshold=settings.gemini_circuit_breaker_threshold,
    cooldown_seconds=settings.gemini_circuit_breaker_cooldown_seconds,
)
