import logging

import pytest

from app.rag.pipeline import AnswerResult
from app.slack.handlers import register_handlers


class _FakeApp:
    """Mimics the slice of slack_bolt.App's decorator API the handlers use, so tests don't
    need a real App (which validates the bot token over the network on construction)."""

    def __init__(self):
        self.events = {}
        self.commands = {}

    def event(self, name):
        def decorator(fn):
            self.events[name] = fn
            return fn

        return decorator

    def command(self, name):
        def decorator(fn):
            self.commands[name] = fn
            return fn

        return decorator


class _FakeSlackClient:
    """Records files_upload_v2 calls; `fail` makes every upload raise, to exercise the fallback."""

    def __init__(self, fail=False):
        self.uploads = []
        self.fail = fail

    def files_upload_v2(self, **kwargs):
        if self.fail:
            raise RuntimeError("missing_scope: files:write")
        self.uploads.append(kwargs)


@pytest.fixture(autouse=True)
def _no_vendor_sessions():
    """app/slack/auth.py holds sessions in a module-level dict; clear it so a `/login` in one
    test can't scope another test's questions."""
    from app.slack import auth

    auth.clear_all()
    yield
    auth.clear_all()


def _register(monkeypatch, authorized=True, result=None):
    calls = []

    def fake_answer_question(question, gemini, **kwargs):
        calls.append({"question": question, "gemini": gemini, **kwargs})
        return result if result is not None else AnswerResult(text="the answer")

    monkeypatch.setattr("app.slack.handlers.answer_question", fake_answer_question)
    monkeypatch.setattr("app.slack.handlers.is_authorized", lambda *a: authorized)

    app = _FakeApp()
    gemini = object()
    register_handlers(app, gemini)
    return app, gemini, calls


def test_mention_passes_the_injected_gemini_client_through(monkeypatch):
    app, gemini, calls = _register(monkeypatch)
    say_calls = []

    app.events["app_mention"](
        {"channel": "C1", "user": "U1", "text": "<@BOT123> how many orders?", "ts": "111.222"},
        lambda **kw: say_calls.append(kw),
        _FakeSlackClient(),
    )

    assert calls == [
        {
            "question": "how many orders?",
            "gemini": gemini,
            "user_id": "U1",
            "channel_id": "C1",
            "authenticated_vendor_id": None,
        }
    ]
    assert say_calls == [{"text": "the answer", "thread_ts": "111.222"}]


def test_mention_unauthorized_sends_no_reply(monkeypatch):
    app, _gemini, calls = _register(monkeypatch, authorized=False)
    say_calls = []

    app.events["app_mention"](
        {"channel": "C1", "user": "U1", "text": "hi", "ts": "1"},
        lambda **kw: say_calls.append(kw),
        _FakeSlackClient(),
    )

    assert calls == []
    assert say_calls == []


def test_dm_passes_the_injected_gemini_client_through(monkeypatch):
    app, gemini, calls = _register(monkeypatch)
    say_calls = []

    app.events["message"](
        {"channel": "D1", "user": "U2", "text": "how many orders?", "channel_type": "im"},
        lambda **kw: say_calls.append(kw),
        _FakeSlackClient(),
    )

    assert calls == [
        {
            "question": "how many orders?",
            "gemini": gemini,
            "user_id": "U2",
            "channel_id": "D1",
            "authenticated_vendor_id": None,
        }
    ]
    assert say_calls == [{"text": "the answer", "thread_ts": None}]


def test_dm_ignores_non_im_and_bot_authored_messages(monkeypatch):
    app, _gemini, calls = _register(monkeypatch)
    client = _FakeSlackClient()

    app.events["message"](
        {"channel": "C1", "channel_type": "channel", "text": "hi"}, lambda **kw: None, client
    )
    app.events["message"](
        {"channel": "D1", "channel_type": "im", "bot_id": "B1", "text": "hi"},
        lambda **kw: None,
        client,
    )

    assert calls == []


def test_ask_command_passes_the_injected_gemini_client_through(monkeypatch):
    app, gemini, calls = _register(monkeypatch)
    acked = []
    responses = []

    app.commands["/ask"](
        ack=lambda: acked.append(True),
        respond=lambda text: responses.append(text),
        command={"channel_id": "C1", "user_id": "U1", "text": "how many orders?"},
        client=_FakeSlackClient(),
    )

    assert acked == [True]
    assert calls == [
        {
            "question": "how many orders?",
            "gemini": gemini,
            "user_id": "U1",
            "channel_id": "C1",
            "authenticated_vendor_id": None,
        }
    ]
    assert responses == ["the answer"]


def test_ask_command_unauthorized_gets_a_visible_denial(monkeypatch):
    app, _gemini, calls = _register(monkeypatch, authorized=False)
    responses = []

    app.commands["/ask"](
        ack=lambda: None,
        respond=lambda text: responses.append(text),
        command={"channel_id": "C1", "user_id": "U1", "text": "how many orders?"},
        client=_FakeSlackClient(),
    )

    assert calls == []
    assert responses == ["Sorry, you're not authorized to use this command here."]


def test_ask_command_empty_question_prompts_for_one(monkeypatch):
    app, _gemini, calls = _register(monkeypatch)
    responses = []

    app.commands["/ask"](
        ack=lambda: None,
        respond=lambda text: responses.append(text),
        command={"channel_id": "C1", "user_id": "U1", "text": "   "},
        client=_FakeSlackClient(),
    )

    assert calls == []
    assert responses == [
        "Please include a question, e.g. `/ask how many orders were placed last week?`"
    ]


def test_a_generated_file_is_uploaded_with_the_answer_as_its_comment(monkeypatch):
    app, _gemini, _calls = _register(
        monkeypatch,
        result=AnswerResult(text="Here's your report.", file_bytes=b"%PDF-", file_type="pdf"),
    )
    client = _FakeSlackClient()
    say_calls = []

    app.events["app_mention"](
        {"channel": "C1", "user": "U1", "text": "pdf report", "ts": "1"},
        lambda **kw: say_calls.append(kw),
        client,
    )

    assert len(client.uploads) == 1
    assert client.uploads[0]["file"] == b"%PDF-"
    assert client.uploads[0]["filename"] == "report.pdf"
    assert client.uploads[0]["initial_comment"] == "Here's your report."
    # The text rides along as the upload's comment -- posting it separately would double it.
    assert say_calls == []


def test_a_failed_upload_still_delivers_the_text_answer(monkeypatch, caplog):
    """The prose is the substance and the file is a convenience: a missing `files:write` scope
    must not turn every report request into silence."""
    app, _gemini, _calls = _register(
        monkeypatch,
        result=AnswerResult(text="Here's your report.", file_bytes=b"%PDF-", file_type="pdf"),
    )
    say_calls = []

    with caplog.at_level(logging.ERROR, logger="audit"):
        app.events["app_mention"](
            {"channel": "C1", "user": "U1", "text": "pdf report", "ts": "1"},
            lambda **kw: say_calls.append(kw),
            _FakeSlackClient(fail=True),
        )

    assert len(say_calls) == 1
    assert "Here's your report." in say_calls[0]["text"]
    assert "couldn't upload" in say_calls[0]["text"]
    # Logged rather than swallowed -- an upload failing on every request is invisible otherwise.
    assert "slack_file_upload_failed" in caplog.text


def test_login_scopes_subsequent_questions_to_that_vendor(monkeypatch):
    app, _gemini, calls = _register(monkeypatch)
    responses = []

    app.commands["/login"](
        ack=lambda: None,
        respond=lambda text: responses.append(text),
        command={"user_id": "U1", "text": "USR-00031"},
    )
    app.events["app_mention"](
        {"channel": "C1", "user": "U1", "text": "how many orders?", "ts": "1"},
        lambda **kw: None,
        _FakeSlackClient(),
    )

    assert "USR-00031" in responses[0]
    assert calls[0]["authenticated_vendor_id"] == "USR-00031"


def test_logout_clears_the_vendor_scope(monkeypatch):
    app, _gemini, calls = _register(monkeypatch)
    responses = []

    app.commands["/login"](
        ack=lambda: None,
        respond=lambda text: None,
        command={"user_id": "U1", "text": "USR-00031"},
    )
    app.commands["/logout"](
        ack=lambda: None,
        respond=lambda text: responses.append(text),
        command={"user_id": "U1"},
    )
    app.events["app_mention"](
        {"channel": "C1", "user": "U1", "text": "how many orders?", "ts": "1"},
        lambda **kw: None,
        _FakeSlackClient(),
    )

    assert "Signed out" in responses[0]
    assert calls[0]["authenticated_vendor_id"] is None
