"""Structured JSON audit logging: one record per answered question."""

import json
import logging
import sys
from datetime import datetime, timezone

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


def configure_logging(level: str = "INFO") -> None:
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)


def log_query_event(
    *,
    question: str,
    duration_ms: float,
    user_id: str | None = None,
    channel_id: str | None = None,
    spec: QuerySpec | None = None,
    error: str | None = None,
    row_count: int | None = None,
    answer: str | None = None,
    timings: dict[str, float] | None = None,
) -> None:
    logger = logging.getLogger(_LOGGER_NAME)
    event = {
        "question": question,
        "user_id": user_id,
        "channel_id": channel_id,
        "query_spec": spec.model_dump() if spec else None,
        "error": error,
        "row_count": row_count,
        "duration_ms": round(duration_ms, 2),
        "timings": timings or {},
        "answer": answer,
    }
    logger.info("query_event", extra={"event": event})
