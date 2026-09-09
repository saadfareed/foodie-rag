"""The contract every state backend implements.

Five operations, no more. The temptation is to expose a general key-value map and let each
guardrail do its own read-modify-write on top; that is precisely what must not happen. Across
replicas, "read the counter, add one, write it back" loses increments under concurrency, and the
guardrail it implements (a daily budget, a rate limit) silently permits more than it should while
still *looking* like it works. So anything that needs to be atomic gets its own primitive, and
the backend is responsible for making it atomic.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class StateBackend(Protocol):
    def get(self, namespace: str, key: str) -> bytes | None:
        """The stored value, or None if absent or expired."""

    def set(
        self, namespace: str, key: str, value: bytes, *, ttl_seconds: float, max_entries: int
    ) -> None:
        """Store `value` for `ttl_seconds`.

        `max_entries` bounds a namespace's size in the in-memory backend (LRU eviction). Redis
        ignores it: TTLs bound the keyspace there, and a server-side eviction policy is the
        operator's decision, not a per-call argument.
        """

    def delete(self, namespace: str, key: str) -> None: ...

    def incr(self, namespace: str, key: str, *, ttl_seconds: float) -> int:
        """Atomically add one and return the new value, setting the TTL on first increment.

        First increment only, so a busy counter's expiry isn't pushed forward on every call --
        a daily budget whose TTL was refreshed per request would never roll over.
        """

    def peek(self, namespace: str, key: str) -> int:
        """A counter's current value, without incrementing it.

        Read-only, and only ever for *reporting* a counter (how much budget is left). Never build
        a read-modify-write on top of it -- that is the race `incr` exists to avoid.
        """

    def allow_in_window(
        self, namespace: str, key: str, *, window_seconds: float, limit: int
    ) -> bool:
        """Atomically: trim the window, and record + allow this call if under `limit`.

        One operation rather than "count, then record", because the gap between those two is
        exactly where two replicas both see limit-1 and both proceed.
        """

    def clear_namespace(self, namespace: str) -> None:
        """Drop every key in one namespace, leaving the others alone.

        Scoped rather than global because "forget every `/login` session" and "forget every
        cached answer" are different operations, and a `clear_all()` that quietly took out the
        rate limiter too would make a test pass for the wrong reason.
        """

    def clear(self) -> None:
        """Drop everything under this backend's control. For tests and local resets."""
