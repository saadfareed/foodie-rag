"""A small, bounded pool for document rendering.

WeasyPrint and openpyxl are the most expensive CPU on the request path -- far more than the
Mongo query, and second only to the Gemini calls in wall clock. Without a bound, every Socket
Mode worker thread (10 by default) can be inside a PDF render at the same time and the box
thrashes: all ten requests get slower, including the nine that would have finished quickly.

Capping renders at `REPORT_RENDER_CONCURRENCY` means the eleventh request queues briefly instead
of degrading the other ten. The Slack worker still blocks waiting for its result -- this bounds
*CPU contention*, not the per-request wall clock -- so `REPORT_RENDER_TIMEOUT_SECONDS` puts a
ceiling on how long that wait can last. On timeout the caller falls back to the text answer,
which is always available, rather than leaving the user with nothing.

Deliberately threads, not processes: WeasyPrint releases the GIL during layout, and a process
pool would mean pickling the row data across a boundary and paying interpreter startup on a
path that is already the slowest thing a user can ask for.
"""

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import TypeVar

from app.config import settings

logger = logging.getLogger("audit")

T = TypeVar("T")


class RenderTimeout(Exception):
    """A render exceeded REPORT_RENDER_TIMEOUT_SECONDS."""


_executor: ThreadPoolExecutor | None = None


def _get_executor() -> ThreadPoolExecutor:
    """Lazily built so importing this module doesn't spawn threads in tests that never render."""
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(
            max_workers=max(1, settings.report_render_concurrency),
            thread_name_prefix="report-render",
        )
    return _executor


def run_render(fn: Callable[[], T], *, description: str = "render") -> T:
    """Run `fn` on the bounded pool and wait up to the configured timeout for it.

    Raises RenderTimeout if the deadline passes, or whatever `fn` raised otherwise.
    """
    future: Future[T] = _get_executor().submit(fn)
    try:
        return future.result(timeout=settings.report_render_timeout_seconds)
    except FutureTimeoutError as exc:
        # The worker thread keeps running to completion -- there is no safe way to interrupt
        # WeasyPrint mid-layout. Cancelling the *wait* is what matters: the user gets a prompt
        # answer, and the orphaned render finishes and is discarded.
        future.cancel()
        logger.warning(
            "report_render_timeout",
            extra={
                "event": {
                    "description": description,
                    "timeout_seconds": settings.report_render_timeout_seconds,
                }
            },
        )
        raise RenderTimeout(
            f"{description} exceeded {settings.report_render_timeout_seconds}s"
        ) from exc


def shutdown() -> None:
    """Release pool threads. Called from the shutdown path in app/main.py."""
    global _executor
    if _executor is not None:
        _executor.shutdown(wait=False, cancel_futures=True)
        _executor = None
