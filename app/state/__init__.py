"""Where every stateful guardrail actually keeps its state.

Until now each of them -- the answer cache, the three per-conversation caches, the rate limiter,
the daily quota, the circuit breaker, the report file store, the `/login` vendor sessions -- held
a module-level `OrderedDict` behind a `threading.Lock`. Correct, fast, and single-replica by
construction: two processes would each enforce their own daily budget, their own rate limit and
their own follow-up context, and a report download would only work on the replica that generated
it. That was CLAUDE.md's headline limitation.

This package is the seam that removes it. Each guardrail keeps its own semantics and its own
public API -- what changed is that the bytes now live behind a `StateBackend`:

* `InMemoryBackend` reproduces exactly the previous behaviour (TTL + per-namespace LRU, in
  process). It is the default, so a single-process deployment is unaffected in every way,
  including not needing Redis installed.
* `RedisBackend` puts the same state in Redis, so N replicas enforce one budget, one rate limit,
  one conversation.

The primitive set is deliberately small and chosen so that *every* operation a guardrail needs is
atomic in Redis. Read-modify-write over `get`/`set` would be a race: two replicas incrementing a
call count would both read 9, both write 10, and the budget would be silently doubled -- which is
the exact class of bug this package exists to fix. So counters go through `incr` and the sliding
window goes through `allow_in_window`, both single round-trips the server serialises.
"""

from app.config import settings
from app.state.backend import StateBackend
from app.state.memory import InMemoryBackend

_backend: StateBackend | None = None


def get_backend() -> StateBackend:
    """The process-wide backend, built on first use from `STATE_BACKEND`.

    Built lazily rather than at import so that importing `app.config` (which every module does)
    never opens a Redis connection -- tests, `ruff`, and the Slack-only deployment shouldn't pay
    for a backend they may not use.
    """
    global _backend
    if _backend is None:
        _backend = _build_backend()
    return _backend


def set_backend(backend: StateBackend | None) -> None:
    """Replace (or, with None, forget) the backend. For tests and for `app.*.main` startup."""
    global _backend
    _backend = backend


def _build_backend() -> StateBackend:
    if settings.state_backend == "redis":
        # Imported here, not at module level: redis is an optional dependency, and a
        # single-replica deployment shouldn't fail to start because it isn't installed.
        from app.state.redis_backend import RedisBackend

        return RedisBackend(url=settings.redis_url, prefix=settings.state_key_prefix)
    return InMemoryBackend()


__all__ = ["InMemoryBackend", "StateBackend", "get_backend", "set_backend"]
