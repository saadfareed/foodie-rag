"""Entrypoint: serve the web chat gateway.

The counterpart to `app/main.py` (which starts the Slack bot), doing the same startup work in the
same order -- logging, index bootstrap, one shared Gemini client, clean shutdown of the render
pool and Mongo client -- because both adapters sit on the same pipeline and neither owns it.

Run with:  python -m app.api.main
"""

import logging

import uvicorn

from app.api.server import create_app
from app.audit.logger import configure_logging
from app.config import settings
from app.db.indexes import ensure_indexes
from app.db.mongo import close_client, get_db
from app.generators.render_pool import shutdown as shutdown_render_pool
from app.llm.gemini_client import GeminiClient


def main() -> None:
    configure_logging(
        settings.audit_log_level,
        log_file=settings.audit_log_file,
        max_bytes=settings.audit_log_file_max_bytes,
        backup_count=settings.audit_log_file_backup_count,
    )
    logger = logging.getLogger("audit")
    try:
        state_error = settings.state_config_error()
        if state_error:
            # Fatal, not a warning: falling back to in-process state when Redis was asked for is
            # the worst outcome available -- the process starts, every request succeeds, and the
            # limits silently stop being shared across replicas.
            raise RuntimeError(state_error)
        replica_warning = settings.single_replica_warning()
        if replica_warning:
            logger.warning("startup_state_warning", extra={"event": {"message": replica_warning}})

        pool_warning = settings.pool_size_warning()
        if pool_warning:
            logger.warning("startup_config_warning", extra={"event": {"message": pool_warning}})

        # Idempotent -- safe on every startup, same as the Slack entrypoint.
        ensure_indexes(get_db())

        # Built once and shared across every request, like app/main.py's -- create_app takes it
        # rather than constructing its own so both adapters reuse one transport.
        app = create_app(GeminiClient())

        logger.info(
            "widget_gateway_starting",
            extra={
                "event": {
                    "host": settings.widget_api_host,
                    "port": settings.widget_api_port,
                    "allowed_origins": settings.widget_allowed_origins or ["*"],
                }
            },
        )
        # Uvicorn installs its own SIGTERM/SIGINT handling and returns from run() on shutdown,
        # so cleanup goes in `finally` rather than in signal handlers like app/main.py needs.
        uvicorn.run(app, host=settings.widget_api_host, port=settings.widget_api_port)
    except Exception:
        logger.exception("Fatal error starting the web chat gateway")
        raise
    finally:
        shutdown_render_pool()
        close_client()


if __name__ == "__main__":
    main()
