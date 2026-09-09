import time

from app.rag.conversation_context import ConversationContextCache


def test_cache_miss_returns_none():
    cache = ConversationContextCache(ttl_seconds=60, max_entries=10)
    assert cache.get(("C1", "U1")) is None


def test_cache_hit_returns_stored_value():
    cache = ConversationContextCache(ttl_seconds=60, max_entries=10)
    cache.set(("C1", "U1"), "how many orders did vendor V1 have last week?")
    assert cache.get(("C1", "U1")) == "how many orders did vendor V1 have last week?"


def test_entry_expires_after_ttl(monkeypatch):
    cache = ConversationContextCache(ttl_seconds=10, max_entries=10)
    cache.set(("C1", "U1"), "how many orders?")
    future = time.monotonic() + 11
    # The TTL clock lives in the state backend now, not in the cache module -- that is the
    # one place expiry is decided for every guardrail (see app/state/memory.py).
    monkeypatch.setattr("app.state.memory.time.monotonic", lambda: future)
    assert cache.get(("C1", "U1")) is None


def test_lru_eviction_drops_least_recently_used():
    cache = ConversationContextCache(ttl_seconds=60, max_entries=2)
    cache.set(("C1", "A"), "a")
    cache.set(("C1", "B"), "b")
    cache.get(("C1", "A"))
    cache.set(("C1", "C"), "c")
    assert cache.get(("C1", "B")) is None
    assert cache.get(("C1", "A")) is not None
    assert cache.get(("C1", "C")) is not None


def test_make_key_scopes_by_channel_and_user_not_question():
    assert ConversationContextCache.make_key("C1", "U1") == ("C1", "U1")
    assert ConversationContextCache.make_key(None, None) == ("", "")


def test_set_overwrites_the_previous_entry_not_appends():
    """A new resolved question always replaces the old one -- this cache never accumulates a
    transcript, so an unrelated follow-on question naturally becomes the new context for the
    *next* turn instead of the old, now-irrelevant one lingering alongside it."""
    cache = ConversationContextCache(ttl_seconds=60, max_entries=10)
    key = ("C1", "U1")
    cache.set(key, "how many orders?")
    cache.set(key, "active vendors in Karachi")
    assert cache.get(key) == "active vendors in Karachi"


def test_clear_removes_the_entry():
    cache = ConversationContextCache(ttl_seconds=60, max_entries=10)
    key = ("C1", "U1")
    cache.set(key, "how many orders?")
    cache.clear(key)
    assert cache.get(key) is None


def test_clear_on_missing_key_does_not_raise():
    cache = ConversationContextCache(ttl_seconds=60, max_entries=10)
    cache.clear(("C1", "U1"))
