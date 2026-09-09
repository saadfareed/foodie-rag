import time

from app.rag.context_switch_cache import ContextSwitchCache, PendingContextSwitch


def test_cache_miss_returns_none():
    cache = ContextSwitchCache(ttl_seconds=60, max_entries=10)
    assert cache.get(("C1", "U1")) is None


def test_cache_hit_returns_stored_value():
    cache = ContextSwitchCache(ttl_seconds=60, max_entries=10)
    cache.set(("C1", "U1"), PendingContextSwitch(candidate_question="how many orders today?"))
    assert cache.get(("C1", "U1")) == PendingContextSwitch(
        candidate_question="how many orders today?"
    )


def test_entry_expires_after_ttl(monkeypatch):
    cache = ContextSwitchCache(ttl_seconds=10, max_entries=10)
    cache.set(("C1", "U1"), PendingContextSwitch(candidate_question="q"))
    future = time.monotonic() + 11
    # The TTL clock lives in the state backend now, not in the cache module -- that is the
    # one place expiry is decided for every guardrail (see app/state/memory.py).
    monkeypatch.setattr("app.state.memory.time.monotonic", lambda: future)
    assert cache.get(("C1", "U1")) is None


def test_lru_eviction_drops_least_recently_used():
    cache = ContextSwitchCache(ttl_seconds=60, max_entries=2)
    cache.set(("C1", "A"), PendingContextSwitch(candidate_question="a"))
    cache.set(("C1", "B"), PendingContextSwitch(candidate_question="b"))
    cache.get(("C1", "A"))
    cache.set(("C1", "C"), PendingContextSwitch(candidate_question="c"))
    assert cache.get(("C1", "B")) is None
    assert cache.get(("C1", "A")) is not None
    assert cache.get(("C1", "C")) is not None


def test_make_key_scopes_by_channel_and_user_not_question():
    assert ContextSwitchCache.make_key("C1", "U1") == ("C1", "U1")
    assert ContextSwitchCache.make_key(None, None) == ("", "")


def test_clear_removes_the_entry():
    cache = ContextSwitchCache(ttl_seconds=60, max_entries=10)
    key = ("C1", "U1")
    cache.set(key, PendingContextSwitch(candidate_question="q"))
    cache.clear(key)
    assert cache.get(key) is None


def test_clear_on_missing_key_does_not_raise():
    cache = ContextSwitchCache(ttl_seconds=60, max_entries=10)
    cache.clear(("C1", "U1"))
