from app.rag.rate_limiter import RateLimiter


def test_disabled_limiter_always_allows():
    limiter = RateLimiter(limit_per_window=0, window_seconds=60)
    key = limiter.make_key("C1", "U1")
    for _ in range(1000):
        assert limiter.allow(key) is True


def test_allows_up_to_the_limit_then_rejects():
    limiter = RateLimiter(limit_per_window=3, window_seconds=60)
    key = limiter.make_key("C1", "U1")

    assert limiter.allow(key) is True
    assert limiter.allow(key) is True
    assert limiter.allow(key) is True
    assert limiter.allow(key) is False


def test_different_keys_have_independent_limits():
    limiter = RateLimiter(limit_per_window=1, window_seconds=60)
    key_a = limiter.make_key("C1", "U1")
    key_b = limiter.make_key("C1", "U2")

    assert limiter.allow(key_a) is True
    assert limiter.allow(key_a) is False
    assert limiter.allow(key_b) is True


def test_old_calls_age_out_of_the_window(monkeypatch):
    import app.rag.rate_limiter as module

    now = [1000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])

    limiter = RateLimiter(limit_per_window=1, window_seconds=10)
    key = limiter.make_key("C1", "U1")

    assert limiter.allow(key) is True
    assert limiter.allow(key) is False

    now[0] += 11  # past the window
    assert limiter.allow(key) is True


def test_tracked_keys_are_bounded_by_lru_eviction():
    limiter = RateLimiter(limit_per_window=5, window_seconds=60, max_tracked_keys=2)

    limiter.allow(limiter.make_key("C1", "U1"))
    limiter.allow(limiter.make_key("C1", "U2"))
    limiter.allow(limiter.make_key("C1", "U3"))

    assert len(limiter._calls) == 2
    assert limiter.make_key("C1", "U1") not in limiter._calls
