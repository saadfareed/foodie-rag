"""In-process TTL + LRU cache for repeated identical questions within a Slack channel.

Avoids repeating identical (and costly) Gemini + MongoDB round-trips when the same question is
asked again in the same channel within a bounded time window. Hand-rolled with
collections.OrderedDict + threading.Lock instead of adding a new dependency -- mirrors
app/llm/quota.py's in-process, thread-safe, module-level-singleton precedent, since Slack Bolt
Socket Mode dispatches handlers via a thread pool (SLACK_SOCKET_MODE_CONCURRENCY).

Cache key: (channel_id, normalized_question) where normalized_question = question.strip().lower().
Scoped per-channel, not per-user or global -- access control (app/slack/access_control.py) is
already channel-scoped, so sharing an answer across users in the same channel adds no new
data-exposure surface.
"""

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from app.config import settings


@dataclass(frozen=True)
class CachedResult:
    """What's replayed on a cache hit."""

    answer: str
    error: str | None = None


class AnswerCache:
    def __init__(self, ttl_seconds: float, max_entries: int) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._lock = threading.Lock()
        # OrderedDict for LRU: move_to_end() on both get and set keeps the most-recently-used
        # entry at the end, so popitem(last=False) evicts the least-recently-used entry first.
        self._entries: OrderedDict[tuple[str, str], tuple[float, CachedResult]] = OrderedDict()

    @staticmethod
    def make_key(channel_id: str | None, question: str) -> tuple[str, str]:
        return (channel_id or "", question.strip().lower())

    def get(self, key: tuple[str, str]) -> CachedResult | None:
        """Returns the cached result, or None on a miss or an expired entry (expired entries are
        evicted immediately on lookup, not left for later eviction pressure)."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            inserted_at, value = entry
            if time.monotonic() - inserted_at > self._ttl_seconds:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return value

    def set(self, key: tuple[str, str], value: CachedResult) -> None:
        with self._lock:
            self._entries[key] = (time.monotonic(), value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)


answer_cache = AnswerCache(
    ttl_seconds=settings.answer_cache_ttl_seconds,
    max_entries=settings.answer_cache_max_entries,
)
