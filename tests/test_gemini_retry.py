import pytest
from google.genai import errors

from app.llm.gemini_client import GeminiClient, _is_retryable


def _client_error(code: int) -> errors.ClientError:
    return errors.ClientError(code, {"error": {"message": "boom"}})


def test_is_retryable_for_429_and_5xx():
    assert _is_retryable(_client_error(429)) is True
    assert _is_retryable(_client_error(503)) is True


def test_is_retryable_false_for_4xx_non_429():
    assert _is_retryable(_client_error(400)) is False
    assert _is_retryable(ValueError("not an api error")) is False


def test_call_with_retry_succeeds_without_retry(monkeypatch):
    monkeypatch.setattr("app.llm.gemini_client.time.sleep", lambda _: None)
    client = GeminiClient.__new__(GeminiClient)
    client.max_retries = 3
    client.retry_base_delay_seconds = 0

    result = client._call_with_retry(lambda: "ok")

    assert result == "ok"


def test_call_with_retry_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr("app.llm.gemini_client.time.sleep", lambda _: None)
    client = GeminiClient.__new__(GeminiClient)
    client.max_retries = 3
    client.retry_base_delay_seconds = 0

    calls = {"count": 0}

    def flaky():
        calls["count"] += 1
        if calls["count"] < 3:
            raise _client_error(429)
        return "recovered"

    result = client._call_with_retry(flaky)

    assert result == "recovered"
    assert calls["count"] == 3


def test_call_with_retry_raises_immediately_on_non_retryable(monkeypatch):
    monkeypatch.setattr("app.llm.gemini_client.time.sleep", lambda _: None)
    client = GeminiClient.__new__(GeminiClient)
    client.max_retries = 3
    client.retry_base_delay_seconds = 0

    calls = {"count": 0}

    def always_bad():
        calls["count"] += 1
        raise _client_error(400)

    with pytest.raises(errors.ClientError):
        client._call_with_retry(always_bad)

    assert calls["count"] == 1


def test_call_with_retry_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr("app.llm.gemini_client.time.sleep", lambda _: None)
    client = GeminiClient.__new__(GeminiClient)
    client.max_retries = 2
    client.retry_base_delay_seconds = 0

    calls = {"count": 0}

    def always_429():
        calls["count"] += 1
        raise _client_error(429)

    with pytest.raises(errors.ClientError):
        client._call_with_retry(always_429)

    assert calls["count"] == 3  # initial attempt + 2 retries
