"""Progress and token events on their way from the pipeline to a browser.

The Slack adapter has no use for this -- a Slack message appears when it is finished -- so the
default sink does nothing and costs nothing, and every existing call path keeps its exact
behaviour. The web adapter passes a real sink and turns the events into SSE.

Two kinds of event, for two different problems:

* **Stages.** A question spends most of its time before the first token exists: classifying,
  resolving anchors, running one query per domain. A spinner for eight seconds and a spinner for
  two seconds look identical, which is what makes the wait feel broken. Naming the stage is the
  cheapest honesty available.
* **Tokens.** The synthesize call is the single longest step, and it is the one that can be shown
  as it happens.

Backpressure is resolved in favour of the worker, not the reader: if a client stops reading, its
queue fills and further progress events are dropped rather than blocking the thread that is
answering the question. The terminal event is the exception -- it is always delivered, because a
reader that misses it waits forever for an answer that already exists.
"""

import queue
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class StreamEvent:
    #: "stage" | "token" | "result" | "error"
    kind: str
    text: str = ""
    data: dict = field(default_factory=dict)


class StreamSink(Protocol):
    def stage(self, name: str, detail: str = "") -> None:
        """Announce that the question has reached a named step."""

    def token(self, text: str) -> None:
        """Emit a piece of the answer. Already redacted -- see app/security/output_scanner.py."""


class NullSink:
    """The default. Every method is a no-op, so nothing that isn't streaming pays for streaming."""

    def stage(self, name: str, detail: str = "") -> None:
        return None

    def token(self, text: str) -> None:
        return None


#: A single shared instance -- it holds no state, so there is no reason to allocate one per
#: request just to call two empty methods on it.
NULL_SINK = NullSink()


class QueueSink:
    """Collects events for a reader on another thread (the SSE endpoint).

    Bounded on purpose. An unbounded queue behind a client that stopped reading is a slow memory
    leak that only shows up under the exact conditions -- flaky mobile connections -- where it is
    hardest to reproduce.
    """

    #: Roughly a long answer's worth of tokens. Past this the reader is not keeping up, and the
    #: useful thing to preserve is the final result, not the backlog.
    _MAX_PENDING = 512

    def __init__(self, max_pending: int | None = None) -> None:
        self._queue: queue.Queue[StreamEvent | None] = queue.Queue(
            maxsize=max_pending or self._MAX_PENDING
        )

    def stage(self, name: str, detail: str = "") -> None:
        self._offer(StreamEvent(kind="stage", text=name, data={"detail": detail} if detail else {}))

    def token(self, text: str) -> None:
        if text:
            self._offer(StreamEvent(kind="token", text=text))

    def finish(self, event: StreamEvent) -> None:
        """Deliver the terminal event and close the stream.

        Makes room by discarding the oldest *progress* events rather than blocking. A reader that
        has fallen behind still needs the answer, and blocking here would strand the worker thread
        on a queue nobody is draining -- a thread leak that only appears with flaky clients, which
        is the worst place for one to appear.
        """
        self._make_room(2)
        self._queue.put_nowait(event)
        self._queue.put_nowait(None)

    def _make_room(self, slots: int) -> None:
        while self._queue.qsize() > max(0, self._queue.maxsize - slots):
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _offer(self, event: StreamEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            # Dropped rather than blocking the thread answering the question. Progress is a
            # courtesy; the answer is the contract.
            pass

    def __iter__(self):
        """Yield events until the terminal one has been delivered."""
        while True:
            event = self._queue.get()
            if event is None:
                return
            yield event
