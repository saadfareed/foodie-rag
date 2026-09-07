import signal

import pytest

from app import main as main_module


class _FakeWebClient:
    """`app.client`, used by the startup scope check. Returns no scopes header, which the check
    reads as "couldn't determine" and passes over -- keeping these tests about main()'s wiring
    rather than about scopes (see tests/test_scopes.py for that)."""

    def auth_test(self):
        return type("_Response", (), {"headers": {}})()


class _FakeApp:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.client = _FakeWebClient()


class _FakeHandler:
    instances = []

    def __init__(self, app, app_token, concurrency=None):
        self.app = app
        self.app_token = app_token
        self.concurrency = concurrency
        self.closed = False
        _FakeHandler.instances.append(self)

    def start(self):
        return None

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    _FakeHandler.instances.clear()
    # main() now ensures DB indexes at startup (app/db/indexes.py) -- stub both out so tests
    # never open a real MongoDB connection.
    monkeypatch.setattr(main_module, "get_db", lambda: "fake-db")
    monkeypatch.setattr(main_module, "ensure_indexes", lambda db: None)
    original_sigterm = signal.getsignal(signal.SIGTERM)
    original_sigint = signal.getsignal(signal.SIGINT)
    yield
    signal.signal(signal.SIGTERM, original_sigterm)
    signal.signal(signal.SIGINT, original_sigint)


def test_main_builds_one_gemini_client_and_injects_it(monkeypatch):
    registered = {}
    close_calls = {"count": 0}

    monkeypatch.setattr(main_module, "configure_logging", lambda level, **kwargs: None)
    monkeypatch.setattr(main_module, "App", _FakeApp)
    monkeypatch.setattr(main_module, "SocketModeHandler", _FakeHandler)
    monkeypatch.setattr(main_module, "GeminiClient", lambda: "the-one-gemini-client")
    monkeypatch.setattr(
        main_module, "register_handlers", lambda app, gemini: registered.update(gemini=gemini)
    )
    monkeypatch.setattr(
        main_module,
        "close_client",
        lambda: close_calls.__setitem__("count", close_calls["count"] + 1),
    )

    main_module.main()

    assert registered["gemini"] == "the-one-gemini-client"
    assert close_calls["count"] >= 1  # cleaned up via the finally-block


def test_shutdown_signal_closes_handler_and_mongo_client_then_exits(monkeypatch):
    close_calls = {"count": 0}

    monkeypatch.setattr(main_module, "configure_logging", lambda level, **kwargs: None)
    monkeypatch.setattr(main_module, "App", _FakeApp)
    monkeypatch.setattr(main_module, "SocketModeHandler", _FakeHandler)
    monkeypatch.setattr(main_module, "GeminiClient", lambda: "gemini")
    monkeypatch.setattr(main_module, "register_handlers", lambda app, gemini: None)
    monkeypatch.setattr(
        main_module,
        "close_client",
        lambda: close_calls.__setitem__("count", close_calls["count"] + 1),
    )

    main_module.main()
    handler = _FakeHandler.instances[-1]

    sigterm_handler = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit):
        sigterm_handler(signal.SIGTERM, None)

    assert handler.closed is True
    assert close_calls["count"] >= 2  # once from the shutdown handler, once from main's finally


def test_main_ensures_db_indexes_at_startup(monkeypatch):
    calls = []
    monkeypatch.setattr(main_module, "configure_logging", lambda level, **kwargs: None)
    monkeypatch.setattr(main_module, "App", _FakeApp)
    monkeypatch.setattr(main_module, "SocketModeHandler", _FakeHandler)
    monkeypatch.setattr(main_module, "GeminiClient", lambda: "gemini")
    monkeypatch.setattr(main_module, "register_handlers", lambda app, gemini: None)
    monkeypatch.setattr(main_module, "close_client", lambda: None)
    monkeypatch.setattr(main_module, "get_db", lambda: "the-db")
    monkeypatch.setattr(main_module, "ensure_indexes", lambda db: calls.append(db))

    main_module.main()

    assert calls == ["the-db"]


def test_main_logs_a_warning_when_the_pool_is_undersized(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(main_module, "configure_logging", lambda level, **kwargs: None)
    monkeypatch.setattr(main_module, "App", _FakeApp)
    monkeypatch.setattr(main_module, "SocketModeHandler", _FakeHandler)
    monkeypatch.setattr(main_module, "GeminiClient", lambda: "gemini")
    monkeypatch.setattr(main_module, "register_handlers", lambda app, gemini: None)
    monkeypatch.setattr(main_module, "close_client", lambda: None)
    monkeypatch.setattr(main_module.settings, "mongodb_max_pool_size", 1)
    monkeypatch.setattr(main_module.settings, "slack_socket_mode_concurrency", 10)
    monkeypatch.setattr(main_module.settings, "agent_max_fan_out", 3)

    with caplog.at_level(logging.WARNING, logger="audit"):
        main_module.main()

    assert any(r.event["message"].startswith("MONGODB_MAX_POOL_SIZE") for r in caplog.records)


def test_socket_mode_concurrency_is_passed_through_from_settings(monkeypatch):
    monkeypatch.setattr(main_module, "configure_logging", lambda level, **kwargs: None)
    monkeypatch.setattr(main_module, "App", _FakeApp)
    monkeypatch.setattr(main_module, "SocketModeHandler", _FakeHandler)
    monkeypatch.setattr(main_module, "GeminiClient", lambda: "gemini")
    monkeypatch.setattr(main_module, "register_handlers", lambda app, gemini: None)
    monkeypatch.setattr(main_module, "close_client", lambda: None)
    monkeypatch.setattr(main_module.settings, "slack_socket_mode_concurrency", 42)

    main_module.main()

    assert _FakeHandler.instances[-1].concurrency == 42
