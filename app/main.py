"""Entrypoint: start the Slack bot in Socket Mode."""

import logging

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from app.audit.logger import configure_logging
from app.config import settings
from app.slack.handlers import register_handlers


def main() -> None:
    configure_logging(settings.audit_log_level)
    try:
        app = App(token=settings.slack_bot_token, signing_secret=settings.slack_signing_secret)
        register_handlers(app)
        SocketModeHandler(app, settings.slack_app_token).start()
    except Exception:
        logging.getLogger("audit").exception("Fatal error starting the Slack bot")
        raise


if __name__ == "__main__":
    main()
