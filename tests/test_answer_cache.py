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
    # The TTL clock lives in the state backend now, not in the cache module -- that is the
    # one place expiry is decided for every guardrail (see app/state/memory.py).
    monkeypatch.setattr("app.state.memory.time.monotonic", lambda: future)
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
    assert AnswerCache.make_key("C1", "  How Many Orders?  ") == (
        "C1",
        "",
        "text",
        "how many orders?",
    )
    assert AnswerCache.make_key(None, "hi") == ("", "", "text", "hi")


def test_make_key_separates_principal_scopes():
    """A vendor-authenticated answer contains only that vendor's rows. Sharing a cache entry
    across identities would replay one vendor's data to another -- a cross-tenant leak, not a
    stale-answer annoyance."""
    shared_question = "how many orders do i have?"
    vendor_a = AnswerCache.make_key("C1", shared_question, principal_scope="vendor:USR-1")
    vendor_b = AnswerCache.make_key("C1", shared_question, principal_scope="vendor:USR-2")
    anonymous = AnswerCache.make_key("C1", shared_question)

    assert vendor_a != vendor_b != anonymous
    assert vendor_a != anonymous


def test_make_key_separates_output_formats():
    """The same words asked as text and as a spreadsheet are different deliverables."""
    assert AnswerCache.make_key("C1", "orders", output_format="text") != AnswerCache.make_key(
        "C1", "orders", output_format="xlsx"
    )


def test_cached_result_round_trips_a_generated_file():
    """A cache that dropped the file replayed a report request as bare prose with the
    attachment silently missing."""
    cache = AnswerCache(ttl_seconds=60, max_entries=10)
    key = AnswerCache.make_key("C1", "orders report", output_format="pdf")
    cache.set(key, CachedResult(answer="Here it is.", file_bytes=b"%PDF-1.7", file_type="pdf"))

    hit = cache.get(key)

    assert hit.file_bytes == b"%PDF-1.7"
    assert hit.file_type == "pdf"
