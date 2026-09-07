"""In-process TTL + LRU cache for repeated identical questions within a Slack channel.

Avoids repeating identical (and costly) Gemini + MongoDB round-trips when the same question is
asked again in the same channel within a bounded time window. Hand-rolled with
collections.OrderedDict + threading.Lock instead of adding a new dependency -- mirrors
app/llm/quota.py's in-process, thread-safe, module-level-singleton precedent, since Slack Bolt
Socket Mode dispatches handlers via a thread pool (SLACK_SOCKET_MODE_CONCURRENCY).

Cache key: (channel_id, vendor_scope, output_format, normalized_question).

Channel scoping alone was not enough once `/login` arrived. A vendor-authenticated question is
answered from *that vendor's rows only* (app/agents/graph.py::_orders_id_filter), so caching it
under the channel meant the next person to ask the same words in the same channel was served
another vendor's data -- a cross-tenant leak, not just a stale answer. `vendor_scope` puts each
authenticated identity in its own cache namespace, and unauthenticated askers in a shared one.

`output_format` is in the key because the same question asked as text and as a PDF are different
deliverables. Without it, "orders by status" cached as text would be replayed to someone who
asked for "orders by status as a csv", and they would get prose and no file.
"""

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from app.config import settings

#: (channel_id, vendor_scope, output_format, normalized_question)
CacheKey = tuple[str, str, str, str]


@dataclass(frozen=True)
class CachedResult:
    """What's replayed on a cache hit -- including any generated file.

    The file bytes are cached alongside the text deliberately: regenerating a PDF is the single
    most expensive thing on the request path, and a cache that stored only the prose meant a
    repeated report request either silently lost its attachment or paid full render cost again.
    """

    answer: str
    error: str | None = None
    file_bytes: bytes | None = None
    file_type: str | None = None


class AnswerCache:
    def __init__(self, ttl_seconds: float, max_entries: int) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._lock = threading.Lock()
        # OrderedDict for LRU: move_to_end() on both get and set keeps the most-recently-used
        # entry at the end, so popitem(last=False) evicts the least-recently-used entry first.
        self._entries: OrderedDict[CacheKey, tuple[float, CachedResult]] = OrderedDict()

    @staticmethod
    def make_key(
        channel_id: str | None,
        question: str,
        *,
        vendor_scope: str | None = None,
        output_format: str = "text",
    ) -> "CacheKey":
        """Build the cache key. See the module docstring for why scope and format are in it."""
        return (
            channel_id or "",
            vendor_scope or "",
            output_format,
            question.strip().lower(),
        )

    def get(self, key: "CacheKey") -> CachedResult | None:
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

    def set(self, key: "CacheKey", value: CachedResult) -> None:
        with self._lock:
            self._entries[key] = (time.monotonic(), value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)


answer_cache = AnswerCache(
    ttl_seconds=settings.answer_cache_ttl_seconds,
    max_entries=settings.answer_cache_max_entries,
)
