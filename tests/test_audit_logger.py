import json
import logging
import sys

import pytest

from app.audit.logger import _JsonFormatter, configure_logging, log_query_event
from app.rag.query_spec import QuerySpec


@pytest.fixture
def _clean_audit_logger():
    """configure_logging mutates the module-level "audit" logger's handler list -- reset it
    around tests that call configure_logging directly so handlers (and open file descriptors)
    from one test don't leak into the next."""
    logger = logging.getLogger("audit")
    original_handlers = list(logger.handlers)
    original_level = logger.level
    for h in original_handlers:
        logger.removeHandler(h)
    yield logger
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    for h in original_handlers:
        logger.addHandler(h)
    logger.setLevel(original_level)


def test_log_query_event_emits_one_record_with_expected_fields(caplog):
    with caplog.at_level(logging.INFO, logger="audit"):
        log_query_event(
            question="how many orders?",
            user_id="U1",
            channel_id="C1",
            specs=[QuerySpec(collection="orders", operation="count")],
            row_count=1,
            duration_ms=12.345,
            answer="42",
        )

    assert len(caplog.records) == 1
    event = caplog.records[0].event
    assert event["question"] == "how many orders?"
    assert event["user_id"] == "U1"
    assert event["channel_id"] == "C1"
    assert event["query_specs"][0]["collection"] == "orders"
    assert event["row_count"] == 1
    assert event["duration_ms"] == 12.35
    assert event["answer"] == "42"
    assert event["error"] is None


def test_log_query_event_with_multiple_specs_lists_them_all(caplog):
    with caplog.at_level(logging.INFO, logger="audit"):
        log_query_event(
            question="vendors near a customer",
            specs=[
                QuerySpec(collection="users", operation="find"),
                QuerySpec(collection="orders", operation="find"),
            ],
            duration_ms=1.0,
        )

    event = caplog.records[0].event
    assert [s["collection"] for s in event["query_specs"]] == ["users", "orders"]


def test_log_query_event_without_specs_leaves_query_specs_none(caplog):
    with caplog.at_level(logging.INFO, logger="audit"):
        log_query_event(question="bad question", error="no data", duration_ms=1.0)

    event = caplog.records[0].event
    assert event["query_specs"] is None
    assert event["error"] == "no data"


def test_log_query_event_records_errors_by_domain_when_given(caplog):
    with caplog.at_level(logging.INFO, logger="audit"):
        log_query_event(
            question="vendors near a customer",
            duration_ms=1.0,
            errors_by_domain={"vendors": "geo query timed out"},
        )

    event = caplog.records[0].event
    assert event["errors_by_domain"] == {"vendors": "geo query timed out"}


def test_log_query_event_records_stage_timings_when_given(caplog):
    with caplog.at_level(logging.INFO, logger="audit"):
        log_query_event(
            question="how many orders?",
            duration_ms=42.0,
            timings={"query_gen_ms": 10.0, "db_ms": 5.0},
        )

    event = caplog.records[0].event
    assert event["timings"] == {"query_gen_ms": 10.0, "db_ms": 5.0}


def test_log_query_event_defaults_timings_to_empty_dict(caplog):
    with caplog.at_level(logging.INFO, logger="audit"):
        log_query_event(question="hi", duration_ms=1.0)

    event = caplog.records[0].event
    assert event["timings"] == {}


def test_configure_logging_writes_json_lines_to_the_configured_file(tmp_path, _clean_audit_logger):
    log_file = tmp_path / "audit.log"
    configure_logging("INFO", log_file=str(log_file))

    log_query_event(question="how many orders?", duration_ms=1.0, answer="42")

    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["question"] == "how many orders?"
    assert record["answer"] == "42"


def test_configure_logging_keeps_stdout_sink_alongside_the_file_sink(tmp_path, _clean_audit_logger):
    configure_logging("INFO", log_file=str(tmp_path / "audit.log"))

    kinds = {type(h).__name__ for h in _clean_audit_logger.handlers}
    assert "StreamHandler" in kinds
    assert "RotatingFileHandler" in kinds


def test_configure_logging_is_idempotent_for_the_same_file(tmp_path, _clean_audit_logger):
    log_file = str(tmp_path / "audit.log")
    configure_logging("INFO", log_file=log_file)
    configure_logging("INFO", log_file=log_file)

    file_handlers = [h for h in _clean_audit_logger.handlers if hasattr(h, "baseFilename")]
    assert len(file_handlers) == 1


def test_configure_logging_without_log_file_only_adds_stdout(_clean_audit_logger):
    configure_logging("INFO")

    kinds = {type(h).__name__ for h in _clean_audit_logger.handlers}
    assert "StreamHandler" in kinds
    assert "RotatingFileHandler" not in kinds


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


def test_an_exception_is_serialized_into_the_audit_record(caplog):
    """Audit records are JSON lines; a traceback has to land inside the payload, not be dropped
    because the formatter only looked at `event`."""
    import json
    import logging

    from app.audit.logger import _JsonFormatter

    formatter = _JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            "audit", logging.ERROR, __file__, 1, "failed", None, sys.exc_info()
        )

    payload = json.loads(formatter.format(record))

    assert "ValueError: boom" in payload["exception"]
    assert payload["message"] == "failed"
