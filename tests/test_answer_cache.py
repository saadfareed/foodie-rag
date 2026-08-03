import time

from app.rag.answer_cache import AnswerCache, CachedResult


def test_cache_miss_returns_none():
    cache = AnswerCache(ttl_seconds=60, max_entries=10)
    assert cache.get(("C1", "hi")) is None


def test_cache_hit_returns_stored_value():
    cache = AnswerCache(ttl_seconds=60, max_entries=10)
    cache.set(("C1", "hi"), CachedResult(answer="hello"))
    assert cache.get(("C1", "hi")) == CachedResult(answer="hello")


def test_entry_expires_after_ttl(monkeypatch):
    cache = AnswerCache(ttl_seconds=10, max_entries=10)
    cache.set(("C1", "hi"), CachedResult(answer="hello"))
    future = time.monotonic() + 11  # captured before patching to avoid self-referential recursion
    monkeypatch.setattr("app.rag.answer_cache.time.monotonic", lambda: future)
    assert cache.get(("C1", "hi")) is None


def test_lru_eviction_drops_least_recently_used():
    cache = AnswerCache(ttl_seconds=60, max_entries=2)
    cache.set(("C1", "a"), CachedResult(answer="A"))
    cache.set(("C1", "b"), CachedResult(answer="B"))
    cache.get(("C1", "a"))  # touch "a" so "b" becomes least-recently-used
    cache.set(("C1", "c"), CachedResult(answer="C"))
    assert cache.get(("C1", "b")) is None
    assert cache.get(("C1", "a")) is not None
    assert cache.get(("C1", "c")) is not None


def test_make_key_normalizes_question_and_defaults_channel():
    assert AnswerCache.make_key("C1", "  How Many Orders?  ") == ("C1", "how many orders?")
    assert AnswerCache.make_key(None, "hi") == ("", "hi")
