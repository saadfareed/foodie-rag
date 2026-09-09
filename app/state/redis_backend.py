"""The shared backend: the same guardrails, enforced once across every replica.

`redis` is an optional dependency, imported here rather than at package level, so a
single-process deployment never needs it installed (see `app/state/__init__.py::_build_backend`).

Two operations are Lua scripts rather than command sequences, and that is the whole point of this
file. `INCR` followed by `EXPIRE` from the client leaves a window where a crash between the two
creates a counter that never expires -- a daily budget stuck at "exhausted" forever. `ZCARD`
followed by `ZADD` leaves a window where two replicas both see limit-1 and both proceed, so a
"10 per minute" limit admits 20. Redis runs a script to completion without interleaving, which
closes both.

The sliding window takes its clock from `TIME` inside the script, not from the caller. Replicas
do not share a clock, and a window assembled from several machines' opinions of "now" is a window
whose length nobody can state.
"""

import time
import uuid

# ARGV: window_seconds, limit, member. Returns 1 to allow, 0 to reject.
_ALLOW_IN_WINDOW = """
local now_parts = redis.call('TIME')
local now = tonumber(now_parts[1]) + (tonumber(now_parts[2]) / 1000000)
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
if redis.call('ZCARD', KEYS[1]) >= limit then
  return 0
end
redis.call('ZADD', KEYS[1], now, ARGV[3])
redis.call('PEXPIRE', KEYS[1], math.ceil(window * 1000))
return 1
"""

# ARGV: ttl_seconds. Returns the new count. TTL is set only when the counter is created, so a
# busy counter's expiry is never pushed forward (see StateBackend.incr).
_INCR_WITH_TTL = """
local value = redis.call('INCR', KEYS[1])
if value == 1 then
  redis.call('PEXPIRE', KEYS[1], math.ceil(tonumber(ARGV[1]) * 1000))
end
return value
"""


class RedisBackend:
    def __init__(self, url: str, prefix: str = "ragchat", client=None) -> None:
        self._prefix = prefix
        if client is not None:
            # Injected for tests -- a fake implementing the handful of commands used here, in
            # keeping with the hand-rolled fakes the rest of the suite uses.
            self._redis = client
        else:
            import redis

            self._redis = redis.Redis.from_url(url)
        self._allow_script = self._redis.register_script(_ALLOW_IN_WINDOW)
        self._incr_script = self._redis.register_script(_INCR_WITH_TTL)

    def _key(self, namespace: str, key: str) -> str:
        return f"{self._prefix}:{namespace}:{key}"

    def get(self, namespace: str, key: str) -> bytes | None:
        return self._redis.get(self._key(namespace, key))

    def set(
        self, namespace: str, key: str, value: bytes, *, ttl_seconds: float, max_entries: int
    ) -> None:
        # max_entries is ignored on purpose: TTLs bound the keyspace here, and how Redis behaves
        # under memory pressure is a server-level policy (maxmemory-policy) an operator sets --
        # not something a per-call argument should be quietly deciding.
        if ttl_seconds <= 0:
            # No expiry, matching the in-memory backend and this codebase's convention that a
            # zero TTL setting disables expiry rather than meaning "expire immediately".
            self._redis.set(self._key(namespace, key), value)
            return
        self._redis.set(self._key(namespace, key), value, px=max(1, int(ttl_seconds * 1000)))

    def delete(self, namespace: str, key: str) -> None:
        self._redis.delete(self._key(namespace, key))

    def incr(self, namespace: str, key: str, *, ttl_seconds: float) -> int:
        return int(self._incr_script(keys=[self._key(namespace, key)], args=[ttl_seconds]))

    def peek(self, namespace: str, key: str) -> int:
        raw = self._redis.get(self._key(namespace, key))
        return int(raw) if raw is not None else 0

    def allow_in_window(
        self, namespace: str, key: str, *, window_seconds: float, limit: int
    ) -> bool:
        if limit <= 0:
            return True
        return bool(
            self._allow_script(
                keys=[self._key(namespace, key)],
                # A unique member per call: the sorted set counts *calls*, and two calls landing
                # on the same millisecond score would otherwise collapse into one entry and
                # quietly raise the effective limit.
                args=[window_seconds, limit, f"{time.time_ns()}-{uuid.uuid4().hex[:8]}"],
            )
        )

    def clear_namespace(self, namespace: str) -> None:
        self._scan_delete(f"{self._prefix}:{namespace}:*")

    def clear(self) -> None:
        """Every key under this prefix. For tests and local resets -- never a request path."""
        self._scan_delete(f"{self._prefix}:*")

    def _scan_delete(self, match: str) -> None:
        """SCAN rather than KEYS, and never FLUSHDB: this backend is a guest in whatever Redis it
        was pointed at, and wiping a database it shares with something else is not its call."""
        cursor = 0
        while True:
            cursor, keys = self._redis.scan(cursor=cursor, match=match, count=500)
            if keys:
                self._redis.delete(*keys)
            if cursor == 0:
                return
