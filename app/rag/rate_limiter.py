"""Per-(channel, user) sliding-window rate limit.

Independent of app/llm/quota.py's daily call budget: that's a *shared* ceiling across every user,
so nothing stops one chatty user from consuming it (or the underlying Gemini free-tier quota, or
a disproportionate share of the Socket Mode thread pool -- app/config.py::
slack_socket_mode_concurrency) alone before anyone else gets a turn. This bounds one identity's
own request rate instead.

Hand-rolled (deque of call timestamps per key, trimmed to the window) rather than a token-bucket
library, mirroring app/rag/answer_cache.py and app/rag/clarification_cache.py's precedent:
in-process, thread-safe, module-level singleton -- same documented single-instance limitation as
those and app/llm/quota.py.
"""

import threading
import time
from collections import OrderedDict, deque

from app.config import settings


class RateLimiter:
    def __init__(
        self, limit_per_window: int, window_seconds: float, max_tracked_keys: int = 1000
    ) -> None:
        self._limit = limit_per_window
        self._window_seconds = window_seconds
        self._max_tracked_keys = max_tracked_keys
        self._lock = threading.Lock()
        # OrderedDict for LRU eviction of tracked keys -- bounds memory even if many distinct
        # (channel, user) pairs show up, the same way answer_cache/clarification_cache do.
        self._calls: OrderedDict[tuple[str, str], deque[float]] = OrderedDict()

    @staticmethod
    def make_key(channel_id: str | None, user_id: str | None) -> tuple[str, str]:
        return (channel_id or "", user_id or "")

    def allow(self, key: tuple[str, str]) -> bool:
        """True if this call may proceed (and is now recorded against the window); False if
        `key` is already at its limit for the current window -- the caller should treat this as
        "reject without doing any real work", not just a warning."""
        if self._limit <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            timestamps = self._calls.get(key)
            if timestamps is None:
                timestamps = deque()
                self._calls[key] = timestamps
            self._calls.move_to_end(key)

            while timestamps and now - timestamps[0] > self._window_seconds:
                timestamps.popleft()

            if len(timestamps) >= self._limit:
                return False

            timestamps.append(now)
            while len(self._calls) > self._max_tracked_keys:
                self._calls.popitem(last=False)
            return True


rate_limiter = RateLimiter(
    limit_per_window=settings.user_rate_limit_per_minute,
    window_seconds=settings.user_rate_limit_window_seconds,
)
