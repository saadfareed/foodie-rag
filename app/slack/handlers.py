"""Slack event/command handlers, all routing into the RAG pipeline."""

import re

from slack_bolt import App

from app.llm.gemini_client import GeminiClient
from app.rag.pipeline import answer_question
from app.slack.access_control import is_authorized

_MENTION_RE = re.compile(r"<@[^>]+>")


def _strip_mention(text: str) -> str:
    return _MENTION_RE.sub("", text).strip()


def register_handlers(app: App, gemini: GeminiClient) -> None:
    """`gemini` is a single shared client constructed once at startup (see `app.main`) --
    handlers must not construct their own, so every question reuses its connection/transport
    instead of paying client-init cost per request."""

    @app.event("app_mention")
    def handle_mention(event: dict, say) -> None:
        channel_id, user_id = event.get("channel"), event.get("user")
        if not is_authorized(channel_id, user_id):
            return
        question = _strip_mention(event.get("text", ""))
        answer = answer_question(question, gemini, user_id=user_id, channel_id=channel_id)
        say(text=answer, thread_ts=event.get("ts"))

    @app.event("message")
    def handle_dm(event: dict, say) -> None:
        if event.get("channel_type") != "im" or event.get("bot_id"):
            return
        channel_id, user_id = event.get("channel"), event.get("user")
        if not is_authorized(channel_id, user_id):
            return
        answer = answer_question(
            event.get("text", ""), gemini, user_id=user_id, channel_id=channel_id
        )
        say(text=answer)

    @app.command("/ask")
    def handle_ask_command(ack, respond, command: dict) -> None:
        ack()
        channel_id, user_id = command.get("channel_id"), command.get("user_id")
        if not is_authorized(channel_id, user_id):
            respond("Sorry, you're not authorized to use this command here.")
            return
        question = command.get("text", "").strip()
        if not question:
            respond("Please include a question, e.g. `/ask how many orders were placed last week?`")
            return
        respond(answer_question(question, gemini, user_id=user_id, channel_id=channel_id))
