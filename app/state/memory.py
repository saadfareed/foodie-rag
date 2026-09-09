"""The in-process backend -- the behaviour every guardrail had before `app/state/` existed.

TTL plus per-namespace LRU, behind one lock, exactly as the hand-rolled `OrderedDict` caches did
it. This is the default, so a single-process deployment gets the previous semantics and needs no
Redis.

`time.monotonic()` throughout, not wall clock: a TTL measured against a clock that can jump
backwards (NTP correction, a VM resuming from suspend) either expires everything at once or
nothing for hours.

Three maps rather than one, because the three primitives store genuinely different things -- a
value, a counter, a window of timestamps -- and a counter serialised into the value map would
have to be parsed on every increment. `delete()` clears a key from all three: they are one
namespace to the caller, and a `delete` that only reached the value map is why
`CircuitBreaker.record_success()` silently failed to reset its own failure count.
"""

import threading
import time
from collections import OrderedDict, deque

#: Bounds the counter and window maps, which have no per-call `max_entries` to size them (the
#: rate limiter's window map grows one entry per distinct (channel, user)). LRU-evicted, matching
#: what the hand-rolled RateLimiter did with its own `max_tracked_keys`.
_MAX_TRACKED_KEYS = 1000


class InMemoryBackend:
    def __init__(self, max_tracked_keys: int = _MAX_TRACKED_KEYS) -> None:
        self._lock = threading.Lock()
        self._max_tracked_keys = max_tracked_keys
        # namespace -> key -> (expires_at, value). Separate OrderedDicts per namespace so one
        # busy namespace's LRU pressure can't evict another's entries -- the previous
        # per-guardrail caches were independent, and collapsing them into one map would quietly
        # couple, say, report downloads to answer-cache churn.
        self._entries: dict[str, OrderedDict[str, tuple[float, bytes]]] = {}
        self._counters: OrderedDict[str, tuple[float, int]] = OrderedDict()
        self._windows: OrderedDict[str, deque[float]] = OrderedDict()

    @staticmethod
    def _expiry(ttl_seconds: float) -> float:
        # ttl <= 0 means "never expires", matching the convention the rest of this codebase uses
        # for a zero setting (VENDOR_SESSION_TTL_SECONDS=0 disables session expiry). Treating it
        # as "expires instantly" would turn that setting into its exact opposite.
        return float("inf") if ttl_seconds <= 0 else time.monotonic() + ttl_seconds

    def get(self, namespace: str, key: str) -> bytes | None:
        with self._lock:
            bucket = self._entries.get(namespace)
            if bucket is None:
                return None
            entry = bucket.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if time.monotonic() > expires_at:
                # Evicted on read rather than left for later eviction pressure, matching what the
                # caches this replaced did.
                del bucket[key]
                return None
            bucket.move_to_end(key)
            return value

    def set(
        self, namespace: str, key: str, value: bytes, *, ttl_seconds: float, max_entries: int
    ) -> None:
        with self._lock:
            bucket = self._entries.setdefault(namespace, OrderedDict())
            bucket[key] = (self._expiry(ttl_seconds), value)
            bucket.move_to_end(key)
            while max_entries > 0 and len(bucket) > max_entries:
                bucket.popitem(last=False)

    def delete(self, namespace: str, key: str) -> None:
        with self._lock:
            bucket = self._entries.get(namespace)
            if bucket is not None:
                bucket.pop(key, None)
            full_key = f"{namespace}:{key}"
            self._counters.pop(full_key, None)
            self._windows.pop(full_key, None)

    def incr(self, namespace: str, key: str, *, ttl_seconds: float) -> int:
        with self._lock:
            full_key = f"{namespace}:{key}"
            entry = self._counters.get(full_key)
            if entry is None or time.monotonic() > entry[0]:
                self._counters[full_key] = (self._expiry(ttl_seconds), 1)
                self._counters.move_to_end(full_key)
                self._evict(self._counters)
                return 1
            expires_at, count = entry
            # Expiry deliberately not extended -- see StateBackend.incr.
            self._counters[full_key] = (expires_at, count + 1)
            self._counters.move_to_end(full_key)
            return count + 1

    def peek(self, namespace: str, key: str) -> int:
        """A counter's current value, without incrementing it.

        Read-only, and only ever for reporting how much budget is left. Never the first half of a
        read-modify-write -- that is the race `incr` exists to avoid.
        """
        with self._lock:
            entry = self._counters.get(f"{namespace}:{key}")
            if entry is None or time.monotonic() > entry[0]:
                return 0
            return entry[1]

    def allow_in_window(
        self, namespace: str, key: str, *, window_seconds: float, limit: int
    ) -> bool:
        if limit <= 0:
            return True
        with self._lock:
            full_key = f"{namespace}:{key}"
            now = time.monotonic()
            timestamps = self._windows.setdefault(full_key, deque())
            self._windows.move_to_end(full_key)
            while timestamps and now - timestamps[0] > window_seconds:
                timestamps.popleft()
            if len(timestamps) >= limit:
                return False
            timestamps.append(now)
            self._evict(self._windows)
            return True

    def _evict(self, mapping: OrderedDict) -> None:
        """Caller holds the lock. Least-recently-touched key goes first."""
        while len(mapping) > self._max_tracked_keys:
            mapping.popitem(last=False)

    def clear_namespace(self, namespace: str) -> None:
        with self._lock:
            self._entries.pop(namespace, None)
            prefix = f"{namespace}:"
            for key in [k for k in self._counters if k.startswith(prefix)]:
                del self._counters[key]
            for key in [k for k in self._windows if k.startswith(prefix)]:
                del self._windows[key]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._counters.clear()
            self._windows.clear()
