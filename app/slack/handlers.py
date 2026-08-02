"""Slack event/command handlers, all routing into the RAG pipeline."""

import re

from slack_bolt import App

from app.rag.pipeline import answer_question

_MENTION_RE = re.compile(r"<@[^>]+>")


def _strip_mention(text: str) -> str:
    return _MENTION_RE.sub("", text).strip()


def register_handlers(app: App) -> None:
    @app.event("app_mention")
    def handle_mention(event: dict, say) -> None:
        question = _strip_mention(event.get("text", ""))
        say(text=answer_question(question), thread_ts=event.get("ts"))

    @app.event("message")
    def handle_dm(event: dict, say) -> None:
        if event.get("channel_type") != "im" or event.get("bot_id"):
            return
        say(text=answer_question(event.get("text", "")))

    @app.command("/ask")
    def handle_ask_command(ack, respond, command: dict) -> None:
        ack()
        question = command.get("text", "").strip()
        if not question:
            respond("Please include a question, e.g. `/ask how many orders were placed last week?`")
            return
        respond(answer_question(question))
