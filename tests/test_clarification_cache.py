import time

from app.rag.clarification_cache import ClarificationCache, PendingClarification


def test_cache_miss_returns_none():
    cache = ClarificationCache(ttl_seconds=60, max_entries=10)
    assert cache.get(("C1", "U1")) is None


def test_cache_hit_returns_stored_value():
    cache = ClarificationCache(ttl_seconds=60, max_entries=10)
    cache.set(("C1", "U1"), PendingClarification(original_question="active vendors", rounds=1))
    assert cache.get(("C1", "U1")) == PendingClarification(
        original_question="active vendors", rounds=1
    )


def test_entry_expires_after_ttl(monkeypatch):
    cache = ClarificationCache(ttl_seconds=10, max_entries=10)
    cache.set(("C1", "U1"), PendingClarification(original_question="q", rounds=1))
    future = time.monotonic() + 11
    monkeypatch.setattr("app.rag.clarification_cache.time.monotonic", lambda: future)
    assert cache.get(("C1", "U1")) is None


def test_lru_eviction_drops_least_recently_used():
    cache = ClarificationCache(ttl_seconds=60, max_entries=2)
    cache.set(("C1", "A"), PendingClarification(original_question="a", rounds=1))
    cache.set(("C1", "B"), PendingClarification(original_question="b", rounds=1))
    cache.get(("C1", "A"))
    cache.set(("C1", "C"), PendingClarification(original_question="c", rounds=1))
    assert cache.get(("C1", "B")) is None
    assert cache.get(("C1", "A")) is not None
    assert cache.get(("C1", "C")) is not None


def test_make_key_scopes_by_channel_and_user_not_question():
    assert ClarificationCache.make_key("C1", "U1") == ("C1", "U1")
    assert ClarificationCache.make_key(None, None) == ("", "")


def test_clear_removes_the_entry():
    cache = ClarificationCache(ttl_seconds=60, max_entries=10)
    key = ("C1", "U1")
    cache.set(key, PendingClarification(original_question="q", rounds=1))
    cache.clear(key)
    assert cache.get(key) is None


def test_clear_on_missing_key_does_not_raise():
    cache = ClarificationCache(ttl_seconds=60, max_entries=10)
    cache.clear(("C1", "U1"))
