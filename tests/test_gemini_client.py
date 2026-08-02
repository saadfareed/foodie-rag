import json

import pytest

from app.llm.gemini_client import GeminiClient, _extract_json, _rows_for_prompt


def test_extract_json_raises_clearly_on_truncated_response():
    """Regression test for a production bug: a too-small max_output_tokens on a "thinking" model
    let reasoning tokens consume the budget before the model finished emitting the JSON object,
    so the response was cut off mid-structure with no closing brace (e.g. '{\\n  "collection').
    _extract_json can't recover a truncated payload, but it must fail with a clear message rather
    than a confusing regex/json internal error."""
    truncated = '{\n  "collection'

    with pytest.raises(ValueError, match="did not contain JSON"):
        _extract_json(truncated)


def test_rows_for_prompt_passes_small_row_sets_through_unmodified():
    rows = [{"amount": 1}, {"amount": 2}]

    result = _rows_for_prompt(rows, max_rows=30)

    assert json.loads(result) == rows
    assert "omitted" not in result


def test_rows_for_prompt_truncates_and_notes_omitted_count():
    rows = [{"i": i} for i in range(50)]

    result = _rows_for_prompt(rows, max_rows=30)

    assert "20 more row(s) omitted" in result
    kept_json = result.split("\n(...")[0]
    assert json.loads(kept_json) == rows[:30]


def test_rows_for_prompt_boundary_equal_to_max_is_not_truncated():
    rows = [{"i": i} for i in range(30)]

    result = _rows_for_prompt(rows, max_rows=30)

    assert "omitted" not in result
    assert json.loads(result) == rows


def test_generate_answer_uses_configured_row_cap(monkeypatch):
    client = GeminiClient.__new__(GeminiClient)
    client.model_name = "test-model"
    client.answer_max_rows = 2

    captured = {}

    class _FakeResponse:
        text = "  the answer  "

    def fake_call_with_retry(fn):
        return fn()

    class _FakeModels:
        def generate_content(self, model, contents, **kwargs):
            captured["model"] = model
            captured["contents"] = contents
            captured["kwargs"] = kwargs
            return _FakeResponse()

    class _FakeClient:
        models = _FakeModels()

    client.client = _FakeClient()
    client._call_with_retry = fake_call_with_retry

    rows = [{"i": i} for i in range(5)]
    answer = client.generate_answer("how many?", rows)

    assert answer == "the answer"
    assert "3 more row(s) omitted" in captured["contents"]


def test_generate_query_spec_uses_low_temperature_json_config(monkeypatch):
    """Query generation should use a deterministic, JSON-mode config -- narrower output (no
    prose to regex-parse) and a token cap trim latency on this specific call."""
    client = GeminiClient.__new__(GeminiClient)
    client.model_name = "test-model"
    client.query_generation_config = "the-configured-config-object"

    captured = {}

    class _FakeResponse:
        text = '{"collection": "orders", "operation": "count"}'

    def fake_call_with_retry(fn):
        return fn()

    class _FakeModels:
        def generate_content(self, model, contents, config=None):
            captured["config"] = config
            return _FakeResponse()

    class _FakeClient:
        models = _FakeModels()

    client.client = _FakeClient()
    client._call_with_retry = fake_call_with_retry

    client.generate_query_spec("how many orders?", "Collection: orders")

    assert captured["config"] == "the-configured-config-object"


def test_client_construction_sets_http_timeout_and_query_config(monkeypatch):
    captured = {}

    class _FakeGenaiClient:
        def __init__(self, *, api_key, http_options):
            captured["api_key"] = api_key
            captured["timeout"] = http_options.timeout

    monkeypatch.setattr("app.llm.gemini_client.genai.Client", _FakeGenaiClient)
    monkeypatch.setattr("app.llm.gemini_client.settings.gemini_api_key", "test-key")
    monkeypatch.setattr("app.llm.gemini_client.settings.gemini_request_timeout_ms", 12345)
    monkeypatch.setattr("app.llm.gemini_client.settings.gemini_query_max_output_tokens", 256)
    monkeypatch.setattr("app.llm.gemini_client.settings.gemini_query_thinking_budget", 0)

    client = GeminiClient()

    assert captured["api_key"] == "test-key"
    assert captured["timeout"] == 12345
    assert client.query_generation_config.temperature == 0
    assert client.query_generation_config.response_mime_type == "application/json"
    assert client.query_generation_config.max_output_tokens == 256
    assert client.query_generation_config.thinking_config.thinking_budget == 0


def test_client_construction_lets_query_thinking_budget_be_tuned(monkeypatch):
    """Escape hatch: if a configured model rejects thinking_budget=0 or benefits from some
    reasoning budget for unusually complex questions, this can be raised via settings without a
    code change."""
    captured = {}

    class _FakeGenaiClient:
        def __init__(self, *, api_key, http_options):
            pass

    monkeypatch.setattr("app.llm.gemini_client.genai.Client", _FakeGenaiClient)
    monkeypatch.setattr("app.llm.gemini_client.settings.gemini_query_thinking_budget", 1024)

    client = GeminiClient()
    captured["budget"] = client.query_generation_config.thinking_config.thinking_budget

    assert captured["budget"] == 1024
