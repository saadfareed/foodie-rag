"""Slack event/command handlers, all routing into the RAG pipeline."""

import logging
import re

from slack_bolt import App

from app.llm.gemini_client import GeminiClient
from app.messages import upload_failure_note
from app.rag.pipeline import AnswerResult, answer_question
from app.slack.access_control import is_authorized
from app.slack.auth import get_principal, login_vendor, logout_vendor

logger = logging.getLogger("audit")

_MENTION_RE = re.compile(r"<@[^>]+>")

# Slack shows the filename, so it should say what the file is rather than "report.bin".
_FILE_TITLES = {
    "csv": "Data export (CSV)",
    "xlsx": "Data export (Excel)",
    "pdf": "Report (PDF)",
}


def _strip_mention(text: str) -> str:
    return _MENTION_RE.sub("", text).strip()


def _missing_scope_from(exc: Exception) -> str | None:
    """The scope Slack said was missing, or None if this failure was something else.

    SlackApiError.response is a SlackResponse (payload on `.data`) in real use, but the API also
    permits a plain dict -- unwrap whichever this is rather than assuming.
    """
    response = getattr(exc, "response", None)
    error = getattr(response, "data", response) or {}
    if isinstance(error, dict) and error.get("error") == "missing_scope":
        return error.get("needed") or "files:write"
    return None


def _send_response(
    result: AnswerResult,
    say=None,
    respond=None,
    client=None,
    channel_id: str | None = None,
    thread_ts: str | None = None,
) -> None:
    """Deliver an answer, uploading its file when there is one.

    A failed upload falls back to the text answer rather than dropping the reply entirely -- the
    prose is the substance and the file is a convenience. The failure is logged rather than
    swallowed: an upload silently failing on every request (a missing `files:write` scope, say)
    is invisible otherwise, and looks to users like the bot simply ignores file requests.
    """
    text = result.text

    if result.has_file and client and channel_id:
        try:
            client.files_upload_v2(
                channel=channel_id,
                file=result.file_bytes,
                filename=f"report.{result.file_type}",
                title=_FILE_TITLES.get(result.file_type, "Report"),
                initial_comment=text,
                thread_ts=thread_ts,
            )
            return
        except Exception as exc:
            logger.exception(
                "slack_file_upload_failed",
                extra={"event": {"channel_id": channel_id, "file_type": result.file_type}},
            )
            text = f"{text}\n\n_{upload_failure_note(result.file_type, _missing_scope_from(exc))}_"

    if say:
        say(text=text, thread_ts=thread_ts)
    elif respond:
        respond(text)


def register_handlers(app: App, gemini: GeminiClient) -> None:
    """`gemini` is a single shared client constructed once at startup (see `app.main`) --
    handlers must not construct their own, so every question reuses its connection/transport
    instead of paying client-init cost per request."""

    @app.event("app_mention")
    def handle_mention(event: dict, say, client) -> None:
        channel_id, user_id = event.get("channel"), event.get("user")
        if not is_authorized(channel_id, user_id):
            return
        result = answer_question(
            _strip_mention(event.get("text", "")),
            gemini,
            user_id=user_id,
            channel_id=channel_id,
            principal=get_principal(user_id),
        )
        _send_response(
            result,
            say=say,
            client=client,
            channel_id=channel_id,
            thread_ts=event.get("ts"),
        )

    @app.event("message")
    def handle_dm(event: dict, say, client) -> None:
        if event.get("channel_type") != "im" or event.get("bot_id"):
            return
        channel_id, user_id = event.get("channel"), event.get("user")
        if not is_authorized(channel_id, user_id):
            return
        result = answer_question(
            event.get("text", ""),
            gemini,
            user_id=user_id,
            channel_id=channel_id,
            principal=get_principal(user_id),
        )
        _send_response(result, say=say, client=client, channel_id=channel_id)

    @app.command("/ask")
    def handle_ask_command(ack, respond, command: dict, client) -> None:
        ack()
        channel_id, user_id = command.get("channel_id"), command.get("user_id")
        if not is_authorized(channel_id, user_id):
            respond("Sorry, you're not authorized to use this command here.")
            return
        question = command.get("text", "").strip()
        if not question:
            respond("Please include a question, e.g. `/ask how many orders were placed last week?`")
            return
        result = answer_question(
            question,
            gemini,
            user_id=user_id,
            channel_id=channel_id,
            principal=get_principal(user_id),
        )
        _send_response(result, respond=respond, client=client, channel_id=channel_id)

    @app.command("/login")
    def handle_login_command(ack, respond, command: dict) -> None:
        ack()
        user_id = command.get("user_id")
        text = command.get("text", "").strip()
        if not text:
            respond("Please provide a vendor ID to log in as. Example: `/login USR-00031`")
            return
        vendor_id = text.split()[0]
        login_vendor(user_id, vendor_id)
        respond(
            f"Signed in as vendor {vendor_id}. You'll only see your own data from now on. "
            "Use `/logout` to end the session."
        )

    @app.command("/logout")
    def handle_logout_command(ack, respond, command: dict) -> None:
        ack()
        if logout_vendor(command.get("user_id")):
            respond("Signed out. You're no longer scoped to a specific vendor.")
        else:
            respond("You weren't signed in as a vendor.")
