import pytest

from app.config import Settings

_REQUIRED_ENV = {
    "SLACK_BOT_TOKEN": "xoxb-test",
    "SLACK_APP_TOKEN": "xapp-test",
    "SLACK_SIGNING_SECRET": "secret",
    "GEMINI_API_KEY": "test-key",
    "MONGODB_URI": "mongodb://localhost",
    "MONGODB_DB_NAME": "testdb",
}


def _set_required_env(monkeypatch):
    for name, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)


def test_missing_required_env_var_names_itself_in_the_error(monkeypatch):
    for name in _REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    _set_required_env(monkeypatch)
    monkeypatch.delenv("MONGODB_DB_NAME", raising=False)

    with pytest.raises(RuntimeError, match="MONGODB_DB_NAME"):
        Settings()


def test_invalid_int_env_var_names_itself_and_the_bad_value(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("GEMINI_MAX_RETRIES", "three")

    with pytest.raises(RuntimeError, match="GEMINI_MAX_RETRIES.*'three'"):
        Settings()


def test_invalid_float_env_var_names_itself_and_the_bad_value(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("GEMINI_RETRY_BASE_DELAY_SECONDS", "soon")

    with pytest.raises(RuntimeError, match="GEMINI_RETRY_BASE_DELAY_SECONDS.*'soon'"):
        Settings()


def test_defaults_are_sane(monkeypatch):
    _set_required_env(monkeypatch)
    for name in (
        "USER_RATE_LIMIT_PER_MINUTE",
        "GEMINI_CIRCUIT_BREAKER_THRESHOLD",
        "GEMINI_CIRCUIT_BREAKER_COOLDOWN_SECONDS",
        "AUDIT_LOG_FILE",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings()

    assert settings.user_rate_limit_per_minute == 0  # disabled by default
    assert settings.gemini_circuit_breaker_threshold == 5
    assert settings.gemini_circuit_breaker_cooldown_seconds == 30.0
    assert settings.audit_log_file == ""


def test_pool_size_warning_none_when_pool_covers_worst_case(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("SLACK_SOCKET_MODE_CONCURRENCY", "10")
    monkeypatch.setenv("AGENT_MAX_FAN_OUT", "3")
    monkeypatch.setenv("MONGODB_MAX_POOL_SIZE", "30")

    assert Settings().pool_size_warning() is None


def test_pool_size_warning_flags_an_undersized_pool(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("SLACK_SOCKET_MODE_CONCURRENCY", "10")
    monkeypatch.setenv("AGENT_MAX_FAN_OUT", "3")
    monkeypatch.setenv("MONGODB_MAX_POOL_SIZE", "5")

    warning = Settings().pool_size_warning()

    assert warning is not None
    assert "MONGODB_MAX_POOL_SIZE" in warning
    assert "30" in warning
