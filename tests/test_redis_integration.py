"""The Redis backend against a real Redis -- opt-in, and skipped by default.

`tests/test_state.py` covers this backend's *wiring* with a fake client: key naming, prefix
scoping, TTL conversion, and the arguments handed to the two Lua scripts. What a fake cannot
cover is the Lua itself, and the Lua is where the guarantees live -- atomic increment, and a
sliding window that admits exactly its limit no matter how many callers race for it.

So these tests exist, and they are skipped unless `REDIS_TEST_URL` is set. That keeps the default
`pytest` run and CI hermetic, which is this suite's standing rule (no live Slack, Mongo or Gemini
calls, ever), while leaving the real verification runnable rather than merely described:

    REDIS_TEST_URL=redis://127.0.0.1:6379/15 pytest tests/test_redis_integration.py

Use a scratch database. Every test clears its own prefix, never the database.
"""

import os
import time

import pytest

from app.api.files import FileStore, StoredFile
from app.llm.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError
from app.llm.quota import QuotaTracker
from app.rag.answer_cache import AnswerCache, CachedResult
from app.rag.conversation_context import ConversationContextCache
from app.rag.rate_limiter import RateLimiter
from app.state.redis_backend import RedisBackend

REDIS_TEST_URL = os.environ.get("REDIS_TEST_URL", "")

pytestmark = pytest.mark.skipif(
    not REDIS_TEST_URL, reason="set REDIS_TEST_URL to run the Redis integration tests"
)


@pytest.fixture
def backend():
    store = RedisBackend(url=REDIS_TEST_URL, prefix="ragchat_test")
    store.clear()
    yield store
    store.clear()


# --- the primitives the Lua scripts implement --------------------------------------------------


def test_incr_is_atomic_and_sets_its_ttl_once(backend):
    """A daily budget whose TTL was refreshed on every call would never roll over."""
    counts = [backend.incr("q", "calls", ttl_seconds=1) for _ in range(3)]
    assert counts == [1, 2, 3]
    assert backend.peek("q", "calls") == 3

    time.sleep(1.3)

    assert backend.peek("q", "calls") == 0


def test_the_window_admits_exactly_its_limit(backend):
    verdicts = [backend.allow_in_window("rl", "u1", window_seconds=60, limit=3) for _ in range(5)]

    assert verdicts == [True, True, True, False, False]


def test_calls_in_the_same_instant_each_count(backend):
    """Two calls landing on the same score would collapse into one sorted-set entry and quietly
    raise the effective limit -- which is why each call adds a unique member."""
    assert all(backend.allow_in_window("rl", "b", window_seconds=60, limit=5) for _ in range(5))
    assert backend.allow_in_window("rl", "b", window_seconds=60, limit=5) is False


def test_the_window_ages_out(backend):
    assert backend.allow_in_window("rl", "u", window_seconds=1, limit=1) is True
    assert backend.allow_in_window("rl", "u", window_seconds=1, limit=1) is False

    time.sleep(1.3)

    assert backend.allow_in_window("rl", "u", window_seconds=1, limit=1) is True


def test_a_non_positive_ttl_persists(backend):
    """Same convention as the in-memory backend, so VENDOR_SESSION_TTL_SECONDS=0 behaves
    identically on both."""
    backend.set("ns", "k", b"v", ttl_seconds=0, max_entries=0)

    assert backend.get("ns", "k") == b"v"


def test_clear_namespace_leaves_other_namespaces_alone(backend):
    backend.set("keep", "a", b"1", ttl_seconds=60, max_entries=0)
    backend.set("drop", "a", b"1", ttl_seconds=60, max_entries=0)

    backend.clear_namespace("drop")

    assert backend.get("drop", "a") is None
    assert backend.get("keep", "a") == b"1"


# --- what this is actually for: two replicas, one set of limits --------------------------------


def test_two_replicas_share_one_daily_budget(backend):
    """The failure this prevents: N replicas spend N times the budget and then meet Google's real
    429 -- the raw upstream error the budget exists to avoid ever showing anyone."""
    replica_a = QuotaTracker(5, backend=backend)
    replica_b = QuotaTracker(5, backend=backend)

    for _ in range(3):
        replica_a.record_call()
    for _ in range(2):
        replica_b.record_call()

    assert replica_a.is_over_budget() and replica_b.is_over_budget()
    assert replica_b.remaining_budget() == 0


def test_two_replicas_share_one_rate_limit_window(backend):
    """ "10 per minute" across four replicas admitting 40 is the quiet version of this bug: every
    rejection message stays accurate, and the number enforced is not the one configured."""
    replica_a = RateLimiter(3, 60.0, backend=backend)
    replica_b = RateLimiter(3, 60.0, backend=backend)
    key = RateLimiter.make_key("web:acme:u1", "u1")

    verdicts = [replica_a.allow(key), replica_b.allow(key), replica_a.allow(key)]

    assert verdicts == [True, True, True]
    assert replica_b.allow(key) is False


def test_one_replicas_outage_opens_the_others_breaker(backend):
    replica_a = CircuitBreaker(2, 60.0, name="gemini-3-flash", backend=backend)
    replica_b = CircuitBreaker(2, 60.0, name="gemini-3-flash", backend=backend)

    replica_a.record_failure()
    replica_a.record_failure()

    with pytest.raises(CircuitBreakerOpenError):
        replica_b.before_call()


def test_a_different_models_breaker_is_unaffected(backend):
    """One model exhausting its daily quota must not trip the breaker for the healthy fallback
    models -- confirmed in production, and the reason each breaker carries a name."""
    CircuitBreaker(1, 60.0, name="gemini-3-flash", backend=backend).record_failure()

    CircuitBreaker(1, 60.0, name="gemini-3.6-flash", backend=backend).before_call()


def test_a_follow_up_keeps_its_context_on_another_replica(backend):
    """Without this the bot appears to forget mid-conversation, intermittently, depending on
    which replica the load balancer picked."""
    key = ConversationContextCache.make_key("web:acme:u1", "u1")
    ConversationContextCache(300, 500, backend=backend).set(key, "how many orders are pending?")

    assert ConversationContextCache(300, 500, backend=backend).get(key) == (
        "how many orders are pending?"
    )


def test_a_cached_report_replays_with_its_bytes_intact(backend):
    """The bytes cross a network hop and a JSON encoding now. A cache that dropped them would
    replay a report request as bare prose with the attachment silently missing -- the exact bug
    AnswerResult was introduced to prevent."""
    key = AnswerCache.make_key(
        "web:acme:u1", "orders by status", principal_scope="vendor:V1", output_format="csv"
    )
    AnswerCache(1800, 500, backend=backend).set(
        key, CachedResult(answer="Here you go.", file_bytes=b"a,b\n1,2\n", file_type="csv")
    )

    hit = AnswerCache(1800, 500, backend=backend).get(key)

    assert hit.file_bytes == b"a,b\n1,2\n"
    assert hit.file_type == "csv"


def test_a_download_works_on_the_replica_that_did_not_generate_it(backend):
    """Behind a load balancer, in-process file storage 404s roughly (N-1)/N of the time."""
    FileStore(600, 200, backend=backend).put(
        "f1", StoredFile("acme", "u1", "csv", "report.csv", b"order_id\nORD-1\n")
    )

    other_replica = FileStore(600, 200, backend=backend)

    assert other_replica.get("f1", tenant_id="acme", principal_id="u1").content == (
        b"order_id\nORD-1\n"
    )
    assert other_replica.get("f1", tenant_id="acme", principal_id="u2") is None
