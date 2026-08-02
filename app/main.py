"""Entrypoint: start the Slack bot in Socket Mode."""

import logging
import signal

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from app.audit.logger import configure_logging
from app.config import settings
from app.db.mongo import close_client
from app.llm.gemini_client import GeminiClient
from app.slack.handlers import register_handlers


def main() -> None:
    configure_logging(settings.audit_log_level)
    try:
        app = App(token=settings.slack_bot_token, signing_secret=settings.slack_signing_secret)
        # Built once and shared across every request -- see app/slack/handlers.py.
        gemini = GeminiClient()
        register_handlers(app, gemini)

        handler = SocketModeHandler(
            app, settings.slack_app_token, concurrency=settings.slack_socket_mode_concurrency
        )

        def _shutdown(signum: int, _frame: object) -> None:
            logging.getLogger("audit").info(
                "shutdown_signal_received", extra={"event": {"signal": signum}}
            )
            handler.close()
            close_client()
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)

        handler.start()
    except Exception:
        logging.getLogger("audit").exception("Fatal error starting the Slack bot")
        raise
    finally:
        close_client()


if __name__ == "__main__":
    main()
