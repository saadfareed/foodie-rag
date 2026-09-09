import pytest

from app.config import settings
from app.llm.circuit_breaker import gemini_circuit_breaker
from app.llm.quota import quota_tracker
from app.rag.rate_limiter import daily_question_limiter, rate_limiter
from app.state import set_backend
from app.state.memory import InMemoryBackend


@pytest.fixture(autouse=True)
def _no_real_database(monkeypatch, request):
    """Fail loudly if a test reaches a real MongoDB instead of a fake.

    Nothing here is supposed to touch the network (see CLAUDE.md's testing conventions), but
    "supposed to" was doing the enforcing, and two rate-limit tests had quietly started relying
    on a live database: they passed on a developer machine with seeded data and failed in CI,
    which is the worst possible arrangement -- green where it's cheap to investigate, red where
    it isn't.

    Patching `get_client` rather than `get_db` puts the tripwire at the actual network boundary,
    so the many tests that legitimately patch `app.agents.graph.get_db` with a fake are
    unaffected and only genuinely-unmocked access trips it.

    `@pytest.mark.uses_mongo_client` opts out, for the one module whose *subject* is
    `get_client` itself (tests/test_mongo.py) -- it stubs the driver a layer lower.
    """
    if request.node.get_closest_marker("uses_mongo_client"):
        return

    def _refuse(*args, **kwargs):
        raise AssertionError(
            "This test reached a real MongoDB. Patch app.agents.graph.get_db with a fake "
            "(see the _FakeDb/_FakeCollection classes in tests/test_pipeline.py). A test that "
            "depends on a live database passes or fails according to what happens to be seeded "
            "on the machine running it."
        )

    monkeypatch.setattr("app.db.mongo.get_client", _refuse)


@pytest.fixture(autouse=True)
def _fresh_state_backend():
    """Give every test its own state.

    This replaces what used to be five separate fixtures, each rebuilding or hand-resetting one
    module-level cache. Every stateful guardrail now keeps its bytes in `app/state`'s backend
    (see that package's docstring), so installing a clean `InMemoryBackend` per test isolates all
    of them at once -- the answer cache, the three conversation caches, the rate-limit window, the
    daily quota counter, the circuit breaker, the report file store, and the `/login` sessions.

    It also closes a gap the per-singleton fixtures had: a test that constructed its own
    `AnswerCache(...)` got isolation, but anything reaching a singleton this fixture list hadn't
    been updated for did not. The rule is now structural -- if it stores state, it stores it here,
    and here is reset.
    """
    set_backend(InMemoryBackend())
    yield
    set_backend(None)


@pytest.fixture(autouse=True)
def _disabled_limits(monkeypatch):
    """Turn off the *configured* limits and their per-role overrides, which are policy, not state.

    `_fresh_state_backend` above empties the counters; these three attributes are the thresholds
    those counters are compared against, and they come from whatever .env the machine happens to
    have. Several tests deliberately drive GeminiClient to fail repeatedly (retry exhaustion,
    non-retryable errors), and with a real threshold configured those failures would trip the
    breaker for unrelated later tests; likewise a finite GEMINI_DAILY_CALL_BUDGET would make tests
    start failing according to how many calls earlier tests happened to make.

    The per-role override maps matter for the same reason and are easy to miss: a developer whose
    .env sets RATE_LIMIT_BY_ROLE=admin:60 would see the rate-limit tests pass while CI, which has
    no .env, sees them fail -- green where it is cheap to investigate, red where it isn't. That is
    the exact failure mode `_no_real_database` above exists to prevent, so the overrides are
    cleared here rather than left to whatever the machine happens to have configured.

    Tests that exercise a limit set its threshold, and its override, themselves.
    """
    monkeypatch.setattr(quota_tracker, "_daily_budget", 0)
    monkeypatch.setattr(rate_limiter, "_limit", 0)
    monkeypatch.setattr(gemini_circuit_breaker, "_failure_threshold", 0)
    monkeypatch.setattr(daily_question_limiter, "_daily_limit", 0)
    monkeypatch.setattr(settings, "rate_limit_by_role", {})
    monkeypatch.setattr(settings, "report_max_rows_by_role", {})
    monkeypatch.setattr(settings, "report_include_contacts", False)
