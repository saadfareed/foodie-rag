"""Structured JSON audit logging: one record per answered question."""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from app.rag.query_spec import QuerySpec

_LOGGER_NAME = "audit"


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        payload.update(getattr(record, "event", {}))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(
    level: str = "INFO",
    log_file: str = "",
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """stdout is always configured (kept for local dev / however the process's stdout happens to
    be captured). `log_file`, if set, adds a second, rotating sink so audit events (question,
    user, specs, errors, timings -- see log_query_event below) survive a container restart
    instead of only existing in whatever ephemerally captured stdout."""
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    # Checked by exact handler type (not isinstance, and not just "logger.handlers is
    # non-empty") -- something else can already be attached to this logger for unrelated reasons
    # (e.g. a test framework's own log-capture handler, which itself subclasses StreamHandler),
    # and either a broader isinstance check or an emptiness check would misread that as "stdout
    # already configured" and silently skip adding the real one.
    has_stdout_handler = any(type(h) is logging.StreamHandler for h in logger.handlers)
    if not has_stdout_handler:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)

    if log_file:
        target = os.path.abspath(log_file)
        already_configured = any(
            getattr(h, "baseFilename", None) == target for h in logger.handlers
        )
        if not already_configured:
            file_handler = RotatingFileHandler(
                log_file, maxBytes=max_bytes, backupCount=backup_count
            )
            file_handler.setFormatter(_JsonFormatter())
            logger.addHandler(file_handler)


def log_query_event(
    *,
    question: str,
    duration_ms: float,
    user_id: str | None = None,
    channel_id: str | None = None,
    specs: list[QuerySpec] | None = None,
    error: str | None = None,
    errors_by_domain: dict[str, str] | None = None,
    row_count: int | None = None,
    answer: str | None = None,
    timings: dict[str, float] | None = None,
    cache_hit: bool = False,
) -> None:
    logger = logging.getLogger(_LOGGER_NAME)
    event = {
        "question": question,
        "user_id": user_id,
        "channel_id": channel_id,
        "query_specs": [spec.model_dump() for spec in specs] if specs else None,
        "error": error,
        "errors_by_domain": errors_by_domain,
        "row_count": row_count,
        "duration_ms": round(duration_ms, 2),
        "timings": timings or {},
        "answer": answer,
        "cache_hit": cache_hit,
    }
    logger.info("query_event", extra={"event": event})
