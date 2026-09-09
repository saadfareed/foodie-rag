"""Daily call budget for Gemini requests, shared by everyone using this deployment.

The counter lives in `app/state` and is keyed by date, so it rolls over on its own rather than
needing a reset check on every read, and it is shared across replicas when STATE_BACKEND=redis.

That sharing is the whole point of the setting. Google's free-tier quota is per *project*, not
per process: three replicas each tracking their own 20-call budget will make 60 calls and then be
surprised by Google's 429, which is exactly the raw upstream error this budget exists to avoid
showing anyone. Counting through `incr` rather than get-then-set matters for the same reason --
two replicas that both read 19 and both write 20 have spent 21 calls against a budget of 20.
"""

from datetime import datetime, timezone

from app.config import settings
from app.state import get_backend

_NAMESPACE = "quota"
#: Comfortably longer than a day, so a counter never expires mid-day, and short enough that
#: yesterday's keys don't accumulate.
_TTL_SECONDS = 48 * 3600


class QuotaTracker:
    def __init__(self, daily_budget: int, backend=None) -> None:
        self._daily_budget = daily_budget
        self._backend = backend

    def _backend_or_default(self):
        return self._backend if self._backend is not None else get_backend()

    @staticmethod
    def _key() -> str:
        # UTC, not local time: replicas in different zones must agree on when "today" starts, or
        # the budget resets somewhere between one and two times a day depending on who is asked.
        return f"calls:{datetime.now(timezone.utc).date().isoformat()}"

    def record_call(self) -> None:
        self._backend_or_default().incr(_NAMESPACE, self._key(), ttl_seconds=_TTL_SECONDS)

    def calls_today(self) -> int:
        return self._backend_or_default().peek(_NAMESPACE, self._key())

    def remaining_budget(self) -> int:
        if self._daily_budget <= 0:
            return -1  # unlimited
        return max(self._daily_budget - self.calls_today(), 0)

    def is_over_budget(self) -> bool:
        if self._daily_budget <= 0:
            return False
        return self.calls_today() >= self._daily_budget


quota_tracker = QuotaTracker(settings.gemini_daily_call_budget)
