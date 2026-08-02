import json
import logging

from app.audit.logger import _JsonFormatter, log_query_event
from app.rag.query_spec import QuerySpec


def test_log_query_event_emits_one_record_with_expected_fields(caplog):
    with caplog.at_level(logging.INFO, logger="audit"):
        log_query_event(
            question="how many orders?",
            user_id="U1",
            channel_id="C1",
            spec=QuerySpec(collection="orders", operation="count"),
            row_count=1,
            duration_ms=12.345,
            answer="42",
        )

    assert len(caplog.records) == 1
    event = caplog.records[0].event
    assert event["question"] == "how many orders?"
    assert event["user_id"] == "U1"
    assert event["channel_id"] == "C1"
    assert event["query_spec"]["collection"] == "orders"
    assert event["row_count"] == 1
    assert event["duration_ms"] == 12.35
    assert event["answer"] == "42"
    assert event["error"] is None


def test_log_query_event_without_spec_leaves_query_spec_none(caplog):
    with caplog.at_level(logging.INFO, logger="audit"):
        log_query_event(question="bad question", error="no data", duration_ms=1.0)

    event = caplog.records[0].event
    assert event["query_spec"] is None
    assert event["error"] == "no data"


def test_json_formatter_produces_valid_json():
    formatter = _JsonFormatter()
    record = logging.LogRecord(
        name="audit",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="query_event",
        args=None,
        exc_info=None,
    )
    record.event = {"question": "hi", "answer": "there"}

    parsed = json.loads(formatter.format(record))

    assert parsed["question"] == "hi"
    assert parsed["answer"] == "there"
    assert parsed["level"] == "INFO"
    assert "timestamp" in parsed
