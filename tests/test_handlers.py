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


def _register(monkeypatch, authorized=True):
    calls = []

    def fake_answer_question(question, gemini, **kwargs):
        calls.append({"question": question, "gemini": gemini, **kwargs})
        return "the answer"

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
    )

    assert calls == [
        {"question": "how many orders?", "gemini": gemini, "user_id": "U1", "channel_id": "C1"}
    ]
    assert say_calls == [{"text": "the answer", "thread_ts": "111.222"}]


def test_mention_unauthorized_sends_no_reply(monkeypatch):
    app, _gemini, calls = _register(monkeypatch, authorized=False)
    say_calls = []

    app.events["app_mention"](
        {"channel": "C1", "user": "U1", "text": "hi", "ts": "1"},
        lambda **kw: say_calls.append(kw),
    )

    assert calls == []
    assert say_calls == []


def test_dm_passes_the_injected_gemini_client_through(monkeypatch):
    app, gemini, calls = _register(monkeypatch)
    say_calls = []

    app.events["message"](
        {"channel": "D1", "user": "U2", "text": "how many orders?", "channel_type": "im"},
        lambda **kw: say_calls.append(kw),
    )

    assert calls == [
        {"question": "how many orders?", "gemini": gemini, "user_id": "U2", "channel_id": "D1"}
    ]
    assert say_calls == [{"text": "the answer"}]


def test_dm_ignores_non_im_and_bot_authored_messages(monkeypatch):
    app, _gemini, calls = _register(monkeypatch)

    app.events["message"](
        {"channel": "C1", "channel_type": "channel", "text": "hi"}, lambda **kw: None
    )
    app.events["message"](
        {"channel": "D1", "channel_type": "im", "bot_id": "B1", "text": "hi"}, lambda **kw: None
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
    )

    assert acked == [True]
    assert calls == [
        {"question": "how many orders?", "gemini": gemini, "user_id": "U1", "channel_id": "C1"}
    ]
    assert responses == ["the answer"]


def test_ask_command_unauthorized_gets_a_visible_denial(monkeypatch):
    app, _gemini, calls = _register(monkeypatch, authorized=False)
    responses = []

    app.commands["/ask"](
        ack=lambda: None,
        respond=lambda text: responses.append(text),
        command={"channel_id": "C1", "user_id": "U1", "text": "how many orders?"},
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
    )

    assert calls == []
    assert responses == [
        "Please include a question, e.g. `/ask how many orders were placed last week?`"
    ]
