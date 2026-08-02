import json

from app.llm.gemini_client import GeminiClient, _rows_for_prompt


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
        def generate_content(self, model, contents):
            captured["model"] = model
            captured["contents"] = contents
            return _FakeResponse()

    class _FakeClient:
        models = _FakeModels()

    client.client = _FakeClient()
    client._call_with_retry = fake_call_with_retry

    rows = [{"i": i} for i in range(5)]
    answer = client.generate_answer("how many?", rows)

    assert answer == "the answer"
    assert "3 more row(s) omitted" in captured["contents"]


def test_client_construction_sets_http_timeout_from_settings(monkeypatch):
    captured = {}

    class _FakeGenaiClient:
        def __init__(self, *, api_key, http_options):
            captured["api_key"] = api_key
            captured["timeout"] = http_options.timeout

    monkeypatch.setattr("app.llm.gemini_client.genai.Client", _FakeGenaiClient)
    monkeypatch.setattr("app.llm.gemini_client.settings.gemini_api_key", "test-key")
    monkeypatch.setattr("app.llm.gemini_client.settings.gemini_request_timeout_ms", 12345)

    GeminiClient()

    assert captured["api_key"] == "test-key"
    assert captured["timeout"] == 12345
