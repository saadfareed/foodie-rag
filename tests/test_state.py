"""The shared state layer (app/state/).

Two things are worth testing here and they are quite different in kind.

The **in-memory backend** is real code with real semantics -- TTL, LRU, atomic-enough counters --
and every guardrail's behaviour now rests on it, so it gets ordinary unit tests.

The **Redis backend** is mostly wiring: key naming, prefix scoping, TTL unit conversion, and
handing the right arguments to two Lua scripts. A fake client can prove all of that. What it
cannot prove is the Lua itself, because implementing a Lua interpreter in a fake would only test
the fake. So the scripts are asserted to be *invoked correctly*, and their contents are verified
against a real Redis, not here -- that limit is deliberate and stated rather than papered over.
"""

import pytest

from app.state.memory import InMemoryBackend
from app.state.redis_backend import RedisBackend
from app.state.store import TtlStore, encode_key

# --- the in-memory backend --------------------------------------------------------------------


def test_a_value_round_trips():
    backend = InMemoryBackend()
    backend.set("ns", "k", b"v", ttl_seconds=60, max_entries=10)

    assert backend.get("ns", "k") == b"v"


def test_an_expired_value_is_gone(monkeypatch):
    backend = InMemoryBackend()
    backend.set("ns", "k", b"v", ttl_seconds=10, max_entries=10)

    import app.state.memory as module

    future = module.time.monotonic() + 11
    monkeypatch.setattr(module.time, "monotonic", lambda: future)

    assert backend.get("ns", "k") is None


def test_a_non_positive_ttl_means_no_expiry(monkeypatch):
    """VENDOR_SESSION_TTL_SECONDS=0 disables session expiry by design. Reading 0 as "expire
    immediately" would turn that setting into its exact opposite."""
    backend = InMemoryBackend()
    backend.set("ns", "k", b"v", ttl_seconds=0, max_entries=10)

    import app.state.memory as module

    monkeypatch.setattr(module.time, "monotonic", lambda: 10**9)

    assert backend.get("ns", "k") == b"v"


def test_namespaces_do_not_collide():
    backend = InMemoryBackend()
    backend.set("a", "k", b"1", ttl_seconds=60, max_entries=10)
    backend.set("b", "k", b"2", ttl_seconds=60, max_entries=10)

    assert backend.get("a", "k") == b"1"
    assert backend.get("b", "k") == b"2"


def test_one_namespaces_lru_pressure_does_not_evict_another():
    """The guardrails were independent caches before this package existed; collapsing them into
    one map would couple report downloads to answer-cache churn."""
    backend = InMemoryBackend()
    backend.set("quiet", "keep", b"1", ttl_seconds=60, max_entries=10)
    for index in range(20):
        backend.set("busy", str(index), b"x", ttl_seconds=60, max_entries=2)

    assert backend.get("quiet", "keep") == b"1"


def test_delete_clears_a_counter_as_well_as_a_value():
    """Counters, values and windows are three maps but one namespace to the caller. A delete that
    reached only the value map is why CircuitBreaker.record_success() silently failed to reset its
    own failure count."""
    backend = InMemoryBackend()
    backend.incr("ns", "k", ttl_seconds=60)
    backend.incr("ns", "k", ttl_seconds=60)
    assert backend.peek("ns", "k") == 2

    backend.delete("ns", "k")

    assert backend.peek("ns", "k") == 0


def test_incr_does_not_extend_its_own_expiry(monkeypatch):
    """A daily budget whose TTL was refreshed on every call would never roll over."""
    backend = InMemoryBackend()
    import app.state.memory as module

    start = module.time.monotonic()
    backend.incr("ns", "k", ttl_seconds=100)

    monkeypatch.setattr(module.time, "monotonic", lambda: start + 50)
    assert backend.incr("ns", "k", ttl_seconds=100) == 2

    monkeypatch.setattr(module.time, "monotonic", lambda: start + 101)
    assert backend.peek("ns", "k") == 0


def test_the_window_admits_exactly_the_limit():
    backend = InMemoryBackend()
    for _ in range(3):
        assert backend.allow_in_window("ns", "k", window_seconds=60, limit=3) is True

    assert backend.allow_in_window("ns", "k", window_seconds=60, limit=3) is False


def test_a_zero_limit_disables_the_window():
    backend = InMemoryBackend()
    for _ in range(100):
        assert backend.allow_in_window("ns", "k", window_seconds=60, limit=0) is True


def test_tracked_windows_are_bounded():
    backend = InMemoryBackend(max_tracked_keys=2)
    for index in range(5):
        backend.allow_in_window("ns", f"user-{index}", window_seconds=60, limit=1)

    assert len(backend._windows) == 2


def test_clear_namespace_leaves_other_namespaces_alone():
    """ "Forget every /login session" and "forget every cached answer" are different operations."""
    backend = InMemoryBackend()
    backend.set("sessions", "u1", b"v", ttl_seconds=60, max_entries=10)
    backend.set("answers", "q1", b"a", ttl_seconds=60, max_entries=10)
    backend.incr("sessions", "count", ttl_seconds=60)

    backend.clear_namespace("sessions")

    assert backend.get("sessions", "u1") is None
    assert backend.peek("sessions", "count") == 0
    assert backend.get("answers", "q1") == b"a"


# --- key encoding -----------------------------------------------------------------------------


def test_key_parts_cannot_be_confused_with_each_other():
    """("a", "b:c") and ("a:b", "c") must not collapse into one key -- a channel id or a question
    can contain any character a naive separator might have used."""
    assert encode_key(("a", "b:c")) != encode_key(("a:b", "c"))


def test_a_long_key_is_hashed_rather_than_stored_whole():
    """The answer cache key embeds the whole question; that does not belong in a key name."""
    encoded = encode_key(("C1", "x" * 500))

    assert encoded.startswith("h:")
    assert len(encoded) < 100


def test_a_short_key_stays_readable():
    assert encode_key(("C1", "U1")) == "C1\x1fU1"


# --- the store --------------------------------------------------------------------------------


def test_unreadable_stored_json_is_a_miss_not_a_crash():
    """Shared state outlives a deploy, so an older or newer replica's encoding turns up here.
    Costing one recomputation is right; raising on the request path is not."""
    backend = InMemoryBackend()
    store = TtlStore("ns", ttl_seconds=60, max_entries=10, backend=backend)
    backend.set("ns", encode_key("k"), b"not json at all", ttl_seconds=60, max_entries=10)

    assert store.get_json("k") is None


# --- the Redis backend (wiring only -- see the module docstring) -------------------------------


class _FakeRedis:
    """The handful of commands RedisBackend uses, and nothing else.

    `register_script` returns a recorder rather than an interpreter: what these tests can honestly
    assert is that the right key and arguments reach the script, not what the script does.
    """

    def __init__(self):
        self.values: dict[str, bytes] = {}
        self.expiries: dict[str, int | None] = {}
        self.script_calls: list[dict] = []

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, px=None):
        self.values[key] = value
        self.expiries[key] = px

    def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)

    def scan(self, cursor=0, match="", count=100):
        import fnmatch

        return 0, [k for k in list(self.values) if fnmatch.fnmatch(k, match)]

    def register_script(self, source):
        def call(keys, args):
            self.script_calls.append({"source": source, "keys": keys, "args": args})
            return 1

        return call


@pytest.fixture
def fake_redis():
    return _FakeRedis()


def test_redis_keys_are_namespaced_and_prefixed(fake_redis):
    """The prefix is what lets this app share a Redis with something else, and what lets clear()
    scope itself instead of reaching for FLUSHDB."""
    backend = RedisBackend(url="", prefix="ragchat", client=fake_redis)
    backend.set("answers", "k", b"v", ttl_seconds=60, max_entries=10)

    assert "ragchat:answers:k" in fake_redis.values


def test_redis_converts_the_ttl_to_milliseconds(fake_redis):
    backend = RedisBackend(url="", prefix="p", client=fake_redis)
    backend.set("ns", "k", b"v", ttl_seconds=1.5, max_entries=0)

    assert fake_redis.expiries["p:ns:k"] == 1500


def test_redis_treats_a_non_positive_ttl_as_persistent(fake_redis):
    """Same convention as the in-memory backend, so a `/login` session with expiry disabled
    behaves identically on both."""
    backend = RedisBackend(url="", prefix="p", client=fake_redis)
    backend.set("ns", "k", b"v", ttl_seconds=0, max_entries=0)

    assert fake_redis.expiries["p:ns:k"] is None


def test_redis_counts_through_a_script_not_get_then_set(fake_redis):
    """Two replicas that both read 19 and both write 20 have spent 21 calls against a budget of
    20. The whole reason `incr` is a primitive is that it must not be assembled client-side."""
    backend = RedisBackend(url="", prefix="p", client=fake_redis)
    backend.incr("quota", "calls:2026-09-08", ttl_seconds=100)

    call = fake_redis.script_calls[-1]
    assert call["keys"] == ["p:quota:calls:2026-09-08"]
    assert "INCR" in call["source"] and "PEXPIRE" in call["source"]


def test_redis_rate_limiting_goes_through_one_atomic_script(fake_redis):
    backend = RedisBackend(url="", prefix="p", client=fake_redis)

    assert backend.allow_in_window("rate_limit", "C1", window_seconds=60, limit=5) is True
    call = fake_redis.script_calls[-1]
    assert call["keys"] == ["p:rate_limit:C1"]
    assert call["args"][:2] == [60, 5]
    assert "ZADD" in call["source"] and "ZREMRANGEBYSCORE" in call["source"]


def test_each_windowed_call_gets_a_distinct_member(fake_redis):
    """Two calls landing on the same score would collapse into one sorted-set entry and quietly
    raise the effective limit."""
    backend = RedisBackend(url="", prefix="p", client=fake_redis)
    backend.allow_in_window("ns", "k", window_seconds=60, limit=5)
    backend.allow_in_window("ns", "k", window_seconds=60, limit=5)

    members = [call["args"][2] for call in fake_redis.script_calls]
    assert members[0] != members[1]


def test_a_zero_limit_never_reaches_redis(fake_redis):
    backend = RedisBackend(url="", prefix="p", client=fake_redis)

    assert backend.allow_in_window("ns", "k", window_seconds=60, limit=0) is True
    assert not fake_redis.script_calls


def test_clear_namespace_only_matches_that_namespace(fake_redis):
    backend = RedisBackend(url="", prefix="p", client=fake_redis)
    backend.set("sessions", "u1", b"v", ttl_seconds=60, max_entries=0)
    backend.set("answers", "q1", b"a", ttl_seconds=60, max_entries=0)

    backend.clear_namespace("sessions")

    assert "p:sessions:u1" not in fake_redis.values
    assert "p:answers:q1" in fake_redis.values
