"""Entrypoint: start the Slack bot in Socket Mode."""

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from app.config import settings
from app.slack.handlers import register_handlers


def main() -> None:
    app = App(token=settings.slack_bot_token, signing_secret=settings.slack_signing_secret)
    register_handlers(app)
    SocketModeHandler(app, settings.slack_app_token).start()


if __name__ == "__main__":
    main()
