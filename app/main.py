"""Entrypoint: start the Slack bot in Socket Mode."""

import logging
import signal

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from app.audit.logger import configure_logging
from app.config import settings
from app.db.indexes import ensure_indexes
from app.db.mongo import close_client, get_db
from app.generators.render_pool import shutdown as shutdown_render_pool
from app.llm.gemini_client import GeminiClient
from app.slack.handlers import register_handlers
from app.slack.scopes import missing_scope_warning


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

        # Two settings in different units that silently cancel each other out when they disagree:
        # a retry budget at or below the per-attempt timeout means slow failures are never
        # retried, which looks exactly like a flaky upstream. See settings.retry_budget_warning().
        retry_warning = settings.retry_budget_warning()
        if retry_warning:
            logger.warning("startup_config_warning", extra={"event": {"message": retry_warning}})

        timeout_warning = settings.gemini_timeout_warning()
        if timeout_warning:
            logger.warning("startup_config_warning", extra={"event": {"message": timeout_warning}})

        # Idempotent -- safe on every startup, not just first-run seeding (see app/db/indexes.py).
        ensure_indexes(get_db())

        app = App(token=settings.slack_bot_token, signing_secret=settings.slack_signing_secret)

        # Checked here rather than discovered when a user asks for a report: a missing scope is
        # invisible until the one API call that needs it is made, which for files:write is the
        # first report request -- long after anyone would connect it to installation.
        scope_warning = missing_scope_warning(app.client)
        if scope_warning:
            logger.warning("startup_scope_warning", extra={"event": {"message": scope_warning}})

        # Built once and shared across every request -- see app/slack/handlers.py.
        gemini = GeminiClient()
        register_handlers(app, gemini)

        handler = SocketModeHandler(
            app, settings.slack_app_token, concurrency=settings.slack_socket_mode_concurrency
        )

        def _shutdown(signum: int, _frame: object) -> None:
            logger.info("shutdown_signal_received", extra={"event": {"signal": signum}})
            handler.close()
            shutdown_render_pool()
            close_client()
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)

        handler.start()
    except Exception:
        logger.exception("Fatal error starting the Slack bot")
        raise
    finally:
        shutdown_render_pool()
        close_client()


if __name__ == "__main__":
    main()
