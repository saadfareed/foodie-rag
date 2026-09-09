"""Progress and token events (app/rag/stream.py).

The interesting behaviour is what happens when the reader stops reading. Everything else here is
a queue.
"""

from app.rag.stream import NULL_SINK, QueueSink, StreamEvent


def _drain(sink: QueueSink) -> list[StreamEvent]:
    return list(sink)


def test_events_arrive_in_order():
    sink = QueueSink()
    sink.stage("understanding")
    sink.token("42 orders")
    sink.finish(StreamEvent(kind="result", text="42 orders are pending."))

    events = _drain(sink)

    assert [e.kind for e in events] == ["stage", "token", "result"]


def test_a_stage_detail_is_carried():
    sink = QueueSink()
    sink.stage("querying", "orders")
    sink.finish(StreamEvent(kind="result"))

    assert _drain(sink)[0].data == {"detail": "orders"}


def test_empty_tokens_are_not_emitted():
    """The redactor returns "" whenever it is holding text back, which is most chunks."""
    sink = QueueSink()
    sink.token("")
    sink.finish(StreamEvent(kind="result"))

    assert [e.kind for e in _drain(sink)] == ["result"]


def test_progress_is_dropped_rather_than_blocking_the_worker():
    """An unbounded queue behind a client that stopped reading is a slow memory leak that only
    appears with flaky connections -- the hardest conditions to reproduce."""
    sink = QueueSink(max_pending=4)
    for index in range(100):
        sink.token(f"chunk-{index}")  # must not block

    sink.finish(StreamEvent(kind="result", text="the answer"))

    assert len(_drain(sink)) <= 4


def test_the_result_is_delivered_even_when_the_queue_is_full():
    """A reader that has fallen behind still needs the answer. Dropping the terminal event would
    leave it waiting forever for something that already exists."""
    sink = QueueSink(max_pending=2)
    for index in range(50):
        sink.token(f"chunk-{index}")

    sink.finish(StreamEvent(kind="result", text="the answer"))
    events = _drain(sink)

    assert events[-1].kind == "result"
    assert events[-1].text == "the answer"


def test_the_null_sink_accepts_everything_and_does_nothing():
    """The Slack path holds this one, so it must never raise and never cost anything."""
    NULL_SINK.stage("understanding", "detail")
    NULL_SINK.token("text")
