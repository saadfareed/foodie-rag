import json

import httpx
import pytest
from google.genai import errors
from pydantic import BaseModel

from app.llm.gemini_client import GeminiClient, _extract_json, _rows_for_prompt
from app.rag.query_spec import QuerySpec


def _rate_limit_error() -> errors.ClientError:
    return errors.ClientError(429, {"error": {"message": "quota exceeded"}})


def _model_not_found_error() -> errors.ClientError:
    return errors.ClientError(404, {"error": {"message": "no longer available to new users"}})


def _server_error(code: int) -> errors.ServerError:
    return errors.ServerError(code, {"error": {"message": "Deadline expired", "status": "X"}})


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
    rows_by_domain = {"orders": [{"amount": 1}, {"amount": 2}]}

    result = _rows_for_prompt(rows_by_domain, max_rows=30)

    assert json.loads(result) == rows_by_domain
    assert "omitted" not in result


def test_rows_for_prompt_truncates_per_domain_and_notes_omitted_count():
    rows_by_domain = {"orders": [{"i": i} for i in range(50)]}

    result = _rows_for_prompt(rows_by_domain, max_rows=30)

    assert "20 more row(s) omitted" in result
    parsed = json.loads(result)
    assert parsed["orders"][:30] == rows_by_domain["orders"][:30]


def test_rows_for_prompt_boundary_equal_to_max_is_not_truncated():
    rows_by_domain = {"orders": [{"i": i} for i in range(30)]}

    result = _rows_for_prompt(rows_by_domain, max_rows=30)

    assert "omitted" not in result
    assert json.loads(result) == rows_by_domain


def test_rows_for_prompt_caps_each_domain_independently():
    """One chatty domain shouldn't crowd another out of the prompt -- the cap applies per
    domain, not to the combined total."""
    rows_by_domain = {"orders": [{"i": i} for i in range(50)], "vendors": [{"i": 1}]}

    result = _rows_for_prompt(rows_by_domain, max_rows=30)
    parsed = json.loads(result)

    assert parsed["vendors"] == [{"i": 1}]
    assert "40 more row(s) omitted" not in result  # only orders was over the cap
    assert "20 more row(s) omitted" in result


def test_generate_answer_uses_configured_row_cap(monkeypatch):
    client = GeminiClient.__new__(GeminiClient)
    client.model_name = "test-model"
    client.fallback_models = []
    client._breakers = {}
    client.max_retry_seconds = 16.0
    client.answer_max_rows = 2

    captured = {}

    class _FakeResponse:
        text = "  the answer  "

    def fake_call_with_retry(fn, breaker=None, deadline=None):
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

    rows_by_domain = {"orders": [{"i": i} for i in range(5)]}
    answer = client.generate_answer("how many?", rows_by_domain)

    assert answer == "the answer"
    assert "3 more row(s) omitted" in captured["contents"]


def _client_for_answer_prompt_capture():
    client = GeminiClient.__new__(GeminiClient)
    client.model_name = "test-model"
    client.fallback_models = []
    client._breakers = {}
    client.max_retry_seconds = 16.0
    client.answer_max_rows = 30

    captured = {}

    class _FakeResponse:
        text = "the answer"

    class _FakeModels:
        def generate_content(self, model, contents, **kwargs):
            captured["contents"] = contents
            return _FakeResponse()

    class _FakeClient:
        models = _FakeModels()

    client.client = _FakeClient()
    client._call_with_retry = lambda fn, breaker=None, deadline=None: fn()
    return client, captured


def test_generate_answer_without_skipped_domains_omits_the_note():
    client, captured = _client_for_answer_prompt_capture()

    client.generate_answer("how many orders?", {"orders": [{"amount": 10}]})

    assert "could not be queried" not in captured["contents"]


def test_generate_answer_with_skipped_domains_includes_them_as_context_not_a_verdict():
    """Regression test for a production bug: a skipped domain used to unconditionally append
    "(Note: I couldn't query X, so that part may be incomplete.)" to the answer regardless of
    whether X's data was actually needed -- e.g. a question asking for a per-vendor sum was fully
    answered using vendor_id from the orders data alone, but the note claimed the answer "may be
    incomplete" anyway. The skipped-domain reason must be handed to the model as context (so it
    can judge relevance), not mechanically appended after the fact."""
    client, captured = _client_for_answer_prompt_capture()

    client.generate_answer(
        "sum of amount each vendor got?",
        {"orders": [{"vendor_id": "V1", "total_amount": 10}]},
        skipped_domains={"vendors": "orders/amounts are outside the vendors schema"},
    )

    prompt = captured["contents"]
    assert "vendors" in prompt
    assert "orders/amounts are outside the vendors schema" in prompt
    assert "Only mention this if it actually leaves" in prompt
    assert "could not be queried" in prompt


def test_generate_structured_uses_low_temperature_json_config():
    """Structured extraction (classification, query-spec generation) should use a deterministic,
    JSON-mode config -- narrower output (no prose to regex-parse) and a token cap trim latency
    on this specific call."""
    client = GeminiClient.__new__(GeminiClient)
    client.model_name = "test-model"
    client.fallback_models = []
    client._breakers = {}
    client.max_retry_seconds = 16.0
    client.query_generation_config = "the-configured-config-object"

    captured = {}

    class _FakeResponse:
        text = '{"collection": "orders", "operation": "count"}'

    def fake_call_with_retry(fn, breaker=None, deadline=None):
        return fn()

    class _FakeModels:
        def generate_content(self, model, contents, config=None):
            captured["config"] = config
            return _FakeResponse()

    class _FakeClient:
        models = _FakeModels()

    client.client = _FakeClient()
    client._call_with_retry = fake_call_with_retry

    result = client.generate_structured("how many orders?", QuerySpec)

    assert captured["config"] == "the-configured-config-object"
    assert result == QuerySpec(collection="orders", operation="count")


def test_generate_structured_or_error_returns_query_error_on_error_shape():
    client = GeminiClient.__new__(GeminiClient)
    client.model_name = "test-model"
    client.fallback_models = []
    client._breakers = {}
    client.max_retry_seconds = 16.0
    client.query_generation_config = None

    class _FakeResponse:
        text = '{"error": "no data for that"}'

    class _FakeModels:
        def generate_content(self, model, contents, config=None):
            return _FakeResponse()

    class _FakeClient:
        models = _FakeModels()

    client.client = _FakeClient()
    client._call_with_retry = lambda fn, breaker=None, deadline=None: fn()

    result = client.generate_structured_or_error("how many orders?", QuerySpec)

    assert result.error == "no data for that"


def test_generate_structured_is_generic_over_any_pydantic_schema():
    class _Widget(BaseModel):
        count: int

    client = GeminiClient.__new__(GeminiClient)
    client.model_name = "test-model"
    client.fallback_models = []
    client._breakers = {}
    client.max_retry_seconds = 16.0
    client.query_generation_config = None

    class _FakeResponse:
        text = '{"count": 7}'

    class _FakeModels:
        def generate_content(self, model, contents, config=None):
            return _FakeResponse()

    class _FakeClient:
        models = _FakeModels()

    client.client = _FakeClient()
    client._call_with_retry = lambda fn, breaker=None, deadline=None: fn()

    assert client.generate_structured("irrelevant prompt", _Widget) == _Widget(count=7)


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


def _client_with_models(model_name, fallback_models, generate_content_fn):
    client = GeminiClient.__new__(GeminiClient)
    client.model_name = model_name
    client.fallback_models = fallback_models
    client._breakers = {}
    client.query_generation_config = None
    client.max_retries = 0
    client.retry_base_delay_seconds = 0
    client.max_retry_seconds = 0

    class _FakeModels:
        def generate_content(self, model, contents, config=None):
            return generate_content_fn(model, config)

    class _FakeClient:
        models = _FakeModels()

    client.client = _FakeClient()
    return client


def test_falls_back_to_next_model_on_rate_limit():
    def generate_content(model, config=None):
        if model == "primary":
            raise _rate_limit_error()

        class _FakeResponse:
            text = '{"count": 1}'

        return _FakeResponse()

    client = _client_with_models("primary", ["fallback"], generate_content)

    class _Widget(BaseModel):
        count: int

    result = client.generate_structured("prompt", _Widget)

    assert result == _Widget(count=1)
    assert client.last_model_used == "fallback"


def test_tries_each_fallback_model_in_order():
    attempts = []

    def generate_content(model, config=None):
        attempts.append(model)
        if model != "third":
            raise _rate_limit_error()

        class _FakeResponse:
            text = '{"count": 1}'

        return _FakeResponse()

    client = _client_with_models("primary", ["second", "third"], generate_content)

    class _Widget(BaseModel):
        count: int

    client.generate_structured("prompt", _Widget)

    assert attempts == ["primary", "second", "third"]


def test_does_not_fall_back_on_a_server_error():
    """A 500 is a server that *failed*, not one that ran out of time. Nothing about it says the
    next model would do better, and cascading through every fallback would multiply latency for a
    failure a different model can't fix."""
    attempts = []

    def generate_content(model, config=None):
        attempts.append(model)
        raise _server_error(500)

    client = _client_with_models("primary", ["fallback"], generate_content)

    class _Widget(BaseModel):
        count: int

    with pytest.raises(errors.ServerError):
        client.generate_structured("prompt", _Widget)

    assert attempts == ["primary"]


def test_falls_back_to_the_next_model_when_the_deadline_is_exceeded():
    """The production failure this came from: a 504 arrived 12.1s into query generation on a
    preview model that had answered a classify call two seconds earlier, and the question failed
    with a fallback model sitting configured and unused. A 504 is a request accepted and then not
    answered in time -- a busy queue -- and a different model is a different queue.
    """
    attempts = []

    def generate_content(model, config=None):
        attempts.append(model)
        if model == "primary":
            raise _server_error(504)

        class _FakeResponse:
            text = '{"count": 1}'

        return _FakeResponse()

    client = _client_with_models("primary", ["fallback"], generate_content)

    class _Widget(BaseModel):
        count: int

    assert client.generate_structured("prompt", _Widget) == _Widget(count=1)
    assert attempts == ["primary", "fallback"]
    assert client.last_model_used == "fallback"


def test_falls_back_to_the_next_model_on_a_client_side_timeout():
    """The same event from our side of the wire. Without this the two halves of one failure are
    handled differently depending on which end noticed first."""
    attempts = []

    def generate_content(model, config=None):
        attempts.append(model)
        if model == "primary":
            raise httpx.ReadTimeout("The read operation timed out")

        class _FakeResponse:
            text = '{"count": 1}'

        return _FakeResponse()

    client = _client_with_models("primary", ["fallback"], generate_content)

    class _Widget(BaseModel):
        count: int

    client.generate_structured("prompt", _Widget)

    assert attempts == ["primary", "fallback"]


def test_raises_the_final_models_error_when_all_are_rate_limited():
    def generate_content(model, config=None):
        raise _rate_limit_error()

    client = _client_with_models("primary", ["fallback"], generate_content)

    class _Widget(BaseModel):
        count: int

    with pytest.raises(errors.ClientError):
        client.generate_structured("prompt", _Widget)


def test_no_fallback_models_configured_behaves_like_before():
    def generate_content(model, config=None):
        raise _rate_limit_error()

    client = _client_with_models("primary", [], generate_content)

    class _Widget(BaseModel):
        count: int

    with pytest.raises(errors.ClientError):
        client.generate_structured("prompt", _Widget)


def test_falls_back_on_a_404_model_not_found_too():
    """Confirmed in practice against a real account: an entire model family can return 404 'no
    longer available to new users' rather than a 429 -- that's just as much a reason to try the
    next configured model as a quota-exhausted 429 is."""

    def generate_content(model, config=None):
        if model == "primary":
            raise _model_not_found_error()

        class _FakeResponse:
            text = '{"count": 1}'

        return _FakeResponse()

    client = _client_with_models("primary", ["fallback"], generate_content)

    class _Widget(BaseModel):
        count: int

    result = client.generate_structured("prompt", _Widget)

    assert result == _Widget(count=1)
    assert client.last_model_used == "fallback"


def test_open_circuit_breaker_on_primary_still_falls_back_to_a_healthy_model(monkeypatch):
    """Regression test for a production incident: circuit breaker state used to be one shared,
    global CircuitBreaker across every model, so repeated real failures against the *primary*
    model (e.g. gemini-3-flash-preview hitting its daily quota) tripped the SAME breaker that
    gemini-3.5-flash-lite (a different, perfectly healthy model) was gated by too -- once open,
    every subsequent call failed immediately with "circuit breaker open", even though the
    fallback model would have succeeded fine. Breaker state must be per-model."""
    monkeypatch.setattr("app.llm.gemini_client.settings.gemini_circuit_breaker_threshold", 2)
    monkeypatch.setattr(
        "app.llm.gemini_client.settings.gemini_circuit_breaker_cooldown_seconds", 999
    )

    def generate_content(model, config=None):
        if model == "primary":
            raise _rate_limit_error()

        class _FakeResponse:
            text = '{"count": 1}'

        return _FakeResponse()

    client = _client_with_models("primary", ["fallback"], generate_content)

    class _Widget(BaseModel):
        count: int

    # Two calls that both fail against "primary" and fall back to "fallback" -- this trips
    # primary's own breaker (threshold=2) without ever calling generate_structured against
    # "fallback" directly, so fallback's breaker stays untouched throughout.
    for _ in range(2):
        result = client.generate_structured("prompt", _Widget)
        assert result == _Widget(count=1)
        assert client.last_model_used == "fallback"

    # A third call: primary's breaker is now open, so _call_with_retry raises
    # CircuitBreakerOpenError for "primary" *without even invoking generate_content for it* --
    # this must still fall through to "fallback", not propagate the breaker error.
    result = client.generate_structured("prompt", _Widget)
    assert result == _Widget(count=1)
    assert client.last_model_used == "fallback"


def test_fallback_attempt_drops_thinking_config_but_keeps_it_for_primary():
    """Confirmed in practice against a real account: gemini-3.6-flash 400s on
    thinking_budget=0, a value the primary model (gemini-3-flash-preview) accepts fine -- a
    fallback attempt should keep everything else from the configured GenerateContentConfig
    except that one tuning knob, which is specific to the primary model's behavior, not a
    general requirement."""
    from google.genai import types

    captured_configs = {}

    def generate_content(model, config=None):
        captured_configs[model] = config
        if model == "primary":
            raise _rate_limit_error()

        class _FakeResponse:
            text = '{"count": 1}'

        return _FakeResponse()

    client = _client_with_models("primary", ["fallback"], generate_content)
    config = types.GenerateContentConfig(
        temperature=0,
        response_mime_type="application/json",
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )

    client._generate_content("prompt", config=config)

    assert captured_configs["primary"].thinking_config.thinking_budget == 0
    assert captured_configs["fallback"].thinking_config is None
    assert captured_configs["fallback"].response_mime_type == "application/json"


# --- answer-prompt pruning -----------------------------------------------------------------
#
# The row cap bounds how many rows go into the answer prompt; these bound how *wide* each row
# is. Without both, 30 documents carrying a long description or an embedded array each are
# still an enormous prompt on the single largest call in the request.


def test_a_long_string_field_is_truncated():
    from app.llm.gemini_client import _prune_value

    pruned = _prune_value("x" * 500, max_chars=200)

    assert len(pruned) == 201  # 200 chars plus the ellipsis
    assert pruned.endswith("…")


def test_a_short_string_is_left_alone():
    from app.llm.gemini_client import _prune_value

    assert _prune_value("cash", max_chars=200) == "cash"


def test_a_long_list_is_summarized():
    from app.llm.gemini_client import _prune_value

    pruned = _prune_value(list(range(20)), max_chars=200)

    assert pruned[:5] == [0, 1, 2, 3, 4]
    assert pruned[-1] == "…15 more"


def test_a_wide_nested_document_is_capped():
    """A GeoJSON polygon or an embedded audit trail contributes thousands of tokens and nothing
    the answer needs."""
    from app.llm.gemini_client import _prune_value

    pruned = _prune_value({f"k{i}": i for i in range(20)}, max_chars=200)

    assert len(pruned) == 8


def test_numbers_and_none_pass_through_unchanged():
    from app.llm.gemini_client import _prune_value

    assert _prune_value(42, max_chars=200) == 42
    assert _prune_value(3.5, max_chars=200) == 3.5
    assert _prune_value(None, max_chars=200) is None


def test_prune_value_recurses_into_a_short_list_without_summarizing_it():
    from app.llm.gemini_client import _prune_value

    assert _prune_value(["a", "y" * 500], max_chars=10) == ["a", "y" * 10 + "…"]


def test_generate_structured_or_error_returns_the_schema_when_there_is_no_error():
    """The error branch is the notable one, but the happy path is what every domain agent
    actually takes."""
    from app.rag.query_spec import QuerySpec

    class _Stub(GeminiClient):
        def __init__(self):
            pass

        def _generate_json(self, prompt):
            return {"collection": "orders", "operation": "find"}

    result = _Stub().generate_structured_or_error("prompt", QuerySpec)

    assert isinstance(result, QuerySpec)
    assert result.collection == "orders"
