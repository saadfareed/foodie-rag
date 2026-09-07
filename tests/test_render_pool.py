"""The bounded render pool: concurrency cap, timeout, and failure propagation.

Uses real threads rather than a mocked executor -- the whole point of this module is what
happens when several renders overlap, and a mock that runs everything inline would test nothing.
Waits are driven by events, not sleeps, so the tests stay fast and don't flake under load.
"""

import logging
import threading
import time

import pytest

from app.generators import render_pool
from app.generators.render_pool import RenderTimeout, run_render, shutdown


@pytest.fixture(autouse=True)
def _fresh_pool():
    """render_pool holds a module-level executor; a pool built with one test's configured
    concurrency would otherwise be reused by every later test."""
    shutdown()
    yield
    shutdown()


def test_returns_the_render_result():
    assert run_render(lambda: b"%PDF-", description="pdf") == b"%PDF-"


def test_runs_off_the_calling_thread():
    """The point of the pool: heavy rendering must not execute on the Slack worker itself."""
    caller = threading.get_ident()

    assert run_render(threading.get_ident) != caller


def test_an_exception_propagates_to_the_caller():
    """A render that fails is the caller's problem to report -- app/rag/pipeline.py turns it
    into a text-only answer. Swallowing it here would hide the failure entirely."""

    def boom():
        raise ValueError("bad table")

    with pytest.raises(ValueError, match="bad table"):
        run_render(boom)


def test_a_slow_render_raises_render_timeout(monkeypatch, caplog):
    monkeypatch.setattr(render_pool.settings, "report_render_timeout_seconds", 0.05)
    release = threading.Event()

    with caplog.at_level(logging.WARNING, logger="audit"):
        with pytest.raises(RenderTimeout, match="slow pdf"):
            run_render(lambda: release.wait(5), description="slow pdf")

    assert "report_render_timeout" in caplog.text
    release.set()  # let the orphaned worker finish so the pool can shut down cleanly


def test_timeout_does_not_kill_the_pool(monkeypatch):
    """The orphaned worker keeps running (WeasyPrint can't be interrupted mid-layout), but the
    pool must still serve the next request."""
    monkeypatch.setattr(render_pool.settings, "report_render_timeout_seconds", 0.05)
    monkeypatch.setattr(render_pool.settings, "report_render_concurrency", 2)
    release = threading.Event()

    with pytest.raises(RenderTimeout):
        run_render(lambda: release.wait(5), description="slow")

    assert run_render(lambda: "ok") == "ok"
    release.set()


def test_concurrency_is_capped_at_the_configured_limit(monkeypatch):
    """Without a cap every Socket Mode worker can be inside WeasyPrint at once and the box
    thrashes -- all requests get slower, including the ones that would have finished fast."""
    monkeypatch.setattr(render_pool.settings, "report_render_concurrency", 2)
    monkeypatch.setattr(render_pool.settings, "report_render_timeout_seconds", 5)

    lock = threading.Lock()
    live = 0
    peak = 0
    release = threading.Event()

    def slow_render():
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        release.wait(3)
        with lock:
            live -= 1
        return None

    threads = [threading.Thread(target=lambda: run_render(slow_render)) for _ in range(6)]
    for thread in threads:
        thread.start()
    # Give the pool time to saturate before releasing, so `peak` reflects the real ceiling.
    time.sleep(0.2)
    release.set()
    for thread in threads:
        thread.join(timeout=5)

    assert peak <= 2, f"{peak} renders ran at once despite a concurrency cap of 2"


def test_shutdown_is_safe_to_call_twice():
    """Called from both the signal handler and the finally-block in app/main.py."""
    run_render(lambda: None)
    shutdown()
    shutdown()

    # And the pool rebuilds lazily for any later call.
    assert run_render(lambda: "rebuilt") == "rebuilt"
