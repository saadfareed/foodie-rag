from app.rag.rate_limiter import DailyQuestionLimiter, RateLimiter
from app.state.memory import InMemoryBackend


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
    # The window itself lives in the state backend now, so that is where the clock is.
    import app.state.memory as state_memory

    now = [1000.0]
    monkeypatch.setattr(state_memory.time, "monotonic", lambda: now[0])

    limiter = RateLimiter(limit_per_window=1, window_seconds=10)
    key = limiter.make_key("C1", "U1")

    assert limiter.allow(key) is True
    assert limiter.allow(key) is False

    now[0] += 11  # past the window
    assert limiter.allow(key) is True


def test_tracked_keys_are_bounded_by_lru_eviction():
    """One window per distinct (channel, user) would grow without limit otherwise. The bound is
    the backend's now (app/state/memory.py), which is where the windows are kept."""
    backend = InMemoryBackend(max_tracked_keys=2)
    limiter = RateLimiter(limit_per_window=1, window_seconds=60, backend=backend)

    assert limiter.allow(limiter.make_key("C1", "U1")) is True
    limiter.allow(limiter.make_key("C1", "U2"))
    limiter.allow(limiter.make_key("C1", "U3"))

    # U1 was evicted, so its window is empty and it gets a fresh allowance -- eviction under
    # pressure is deliberately generous rather than a silent permanent block.
    assert limiter.allow(limiter.make_key("C1", "U1")) is True


# --- per-role limits ------------------------------------------------------------------------


def test_a_role_override_replaces_the_configured_limit():
    """An operator answering a support question and a customer poking at a chat widget are not
    the same load, and one flat number has to be set for the worst of them."""
    limiter = RateLimiter(limit_per_window=1, window_seconds=60)
    key = limiter.make_key("C1", "U1")

    assert limiter.allow(key, limit=3) is True
    assert limiter.allow(key, limit=3) is True
    assert limiter.allow(key, limit=3) is True
    assert limiter.allow(key, limit=3) is False


def test_a_role_override_cannot_re_enable_a_disabled_limiter():
    """USER_RATE_LIMIT_PER_MINUTE=0 is the master switch. A deployment that deliberately turned
    rate limiting off must not silently regain it by naming a role."""
    limiter = RateLimiter(limit_per_window=0, window_seconds=60)
    key = limiter.make_key("C1", "U1")

    for _ in range(50):
        assert limiter.allow(key, limit=1) is True


def test_no_override_uses_the_limiters_own_limit():
    limiter = RateLimiter(limit_per_window=2, window_seconds=60)
    key = limiter.make_key("C1", "U1")

    assert [limiter.allow(key), limiter.allow(key), limiter.allow(key)] == [True, True, False]


# --- the daily limit ------------------------------------------------------------------------


def test_the_daily_limit_admits_exactly_its_allowance():
    """The window above stops a burst. This stops a slow drain -- one question every thirty
    seconds passes every per-minute check and still exhausts a free-tier quota by lunchtime."""
    limiter = DailyQuestionLimiter(daily_limit=3)

    verdicts = [limiter.allow("vendor:USR-1") for _ in range(5)]

    assert verdicts == [True, True, True, False, False]


def test_the_daily_limit_is_per_principal():
    limiter = DailyQuestionLimiter(daily_limit=1)

    assert limiter.allow("vendor:USR-1") is True
    assert limiter.allow("vendor:USR-2") is True
    assert limiter.allow("vendor:USR-1") is False


def test_the_daily_limit_separates_roles_sharing_an_id():
    """`cache_scope` is role:id, and the same id under two roles is two different people as far
    as what they can see goes."""
    limiter = DailyQuestionLimiter(daily_limit=1)

    assert limiter.allow("vendor:USR-1") is True
    assert limiter.allow("customer:USR-1") is True


def test_a_zero_daily_limit_is_disabled():
    limiter = DailyQuestionLimiter(daily_limit=0)

    for _ in range(100):
        assert limiter.allow("vendor:USR-1") is True


def test_the_daily_limit_rolls_over_by_utc_date(monkeypatch):
    """Keyed by date so it resets on its own, and UTC so replicas in different zones agree on
    when "today" starts."""
    from datetime import datetime, timedelta, timezone

    limiter = DailyQuestionLimiter(daily_limit=1)
    assert limiter.allow("vendor:USR-1") is True
    assert limiter.allow("vendor:USR-1") is False

    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)

    class _FrozenClock:
        @staticmethod
        def now(tz=None):
            return tomorrow

    monkeypatch.setattr("app.rag.rate_limiter.datetime", _FrozenClock)

    assert limiter.allow("vendor:USR-1") is True


def test_the_daily_limit_counts_and_checks_in_one_step():
    """Counted through the backend's atomic incr, so a burst can't slip several past the line
    together the way a read-then-write could."""
    limiter = DailyQuestionLimiter(daily_limit=5)
    for _ in range(3):
        limiter.allow("vendor:USR-1")

    assert limiter.used_today("vendor:USR-1") == 3
