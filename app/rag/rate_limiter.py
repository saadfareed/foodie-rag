"""Bounding what one identity can consume: a sliding window, and a daily count.

Two limits, because they stop different things. The window stops a burst -- a script, a stuck
retry loop, someone holding down enter. The daily count stops a slow drain: a single identity
asking a question every thirty seconds all day, which no per-minute limit would ever notice and
which will still exhaust a free-tier quota by lunchtime.

Both are independent of app/llm/quota.py's daily call budget: that is a *shared* ceiling across
every user, so nothing stops one chatty user from consuming it (or the underlying Gemini
free-tier quota, or a disproportionate share of the worker pool) alone before anyone else gets a
turn. These bound one identity's own usage instead.

The window itself lives in `app/state`, which is what makes the limit mean the same thing however
many replicas are running. Held in-process, "10 per minute" across four replicas admits 40 -- the
limit still *looks* enforced, every rejection message is still correct, and the number it enforces
is silently four times the one configured. That is the specific failure this now avoids, and it is
why the backend exposes `allow_in_window` as a single atomic operation rather than letting this
module count and then record.
"""

from datetime import datetime, timezone

from app.config import settings
from app.state import get_backend
from app.state.store import encode_key

_NAMESPACE = "rate_limit"
_DAILY_NAMESPACE = "daily_questions"
#: Comfortably longer than a day so a counter never expires mid-day, short enough that
#: yesterday's keys don't accumulate.
_DAILY_TTL_SECONDS = 48 * 3600


class RateLimiter:
    def __init__(self, limit_per_window: int, window_seconds: float, backend=None) -> None:
        self._limit = limit_per_window
        self._window_seconds = window_seconds
        self._backend = backend

    @staticmethod
    def make_key(channel_id: str | None, user_id: str | None) -> tuple[str, str]:
        return (channel_id or "", user_id or "")

    def allow(self, key: tuple[str, str], limit: int | None = None) -> bool:
        """True if this call may proceed (and is now recorded against the window); False if
        `key` is already at its limit -- the caller should treat this as "reject without doing any
        real work", not just a warning.

        `limit` overrides the configured one for this call, which is how per-role limits work
        (see `settings.rate_limit_for`). The configured limit remains the master switch: when it
        is 0 the limiter is off and an override cannot turn it back on, so a deployment that
        deliberately disabled rate limiting doesn't silently regain it by naming a role.
        """
        if self._limit <= 0:
            return True
        effective = self._limit if limit is None else limit
        if effective <= 0:
            return True
        backend = self._backend if self._backend is not None else get_backend()
        return backend.allow_in_window(
            _NAMESPACE,
            encode_key(key),
            window_seconds=self._window_seconds,
            limit=effective,
        )


class DailyQuestionLimiter:
    """How many questions one identity may ask per UTC day.

    Keyed by date so it rolls over on its own rather than needing a reset anyone has to remember,
    and counted through the backend's atomic `incr` for the same reason the shared quota is: two
    replicas that both read 19 and both write 20 have let through 21.

    UTC, not local time -- replicas in different zones must agree on when "today" starts, or the
    allowance resets somewhere between one and two times a day depending on who is asked.
    """

    def __init__(self, daily_limit: int, backend=None) -> None:
        self._daily_limit = daily_limit
        self._backend = backend

    @staticmethod
    def make_key(principal_scope: str) -> str:
        return f"{principal_scope}:{datetime.now(timezone.utc).date().isoformat()}"

    def allow(self, principal_scope: str) -> bool:
        """True if this question may proceed, and counts it. False once the day's allowance is
        spent -- checked and recorded in one atomic step, so a burst cannot slip several past the
        line together."""
        if self._daily_limit <= 0:
            return True
        backend = self._backend if self._backend is not None else get_backend()
        used = backend.incr(
            _DAILY_NAMESPACE, self.make_key(principal_scope), ttl_seconds=_DAILY_TTL_SECONDS
        )
        return used <= self._daily_limit

    def used_today(self, principal_scope: str) -> int:
        backend = self._backend if self._backend is not None else get_backend()
        return backend.peek(_DAILY_NAMESPACE, self.make_key(principal_scope))


rate_limiter = RateLimiter(
    limit_per_window=settings.user_rate_limit_per_minute,
    window_seconds=settings.user_rate_limit_window_seconds,
)


daily_question_limiter = DailyQuestionLimiter(settings.user_daily_question_limit)
