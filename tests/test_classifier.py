import pytest
from pydantic import ValidationError

from app.agents.classifier import Classification, classify_question


class _StubGemini:
    def __init__(self, classification: Classification):
        self._classification = classification
        self.prompts = []

    def generate_structured(self, prompt, schema):
        self.prompts.append(prompt)
        assert schema is Classification
        return self._classification


def test_classify_question_returns_the_gemini_result():
    expected = Classification(domains=["vendors"], needs_geo=True, confidence=0.8)
    gemini = _StubGemini(expected)

    result = classify_question(gemini, "active vendors nearby")

    assert result == expected


def test_classify_question_prompt_includes_the_question_and_domain_names():
    gemini = _StubGemini(Classification(domains=["orders"], confidence=0.9))

    classify_question(gemini, "how many pending orders")

    prompt = gemini.prompts[0]
    assert "how many pending orders" in prompt
    assert "orders" in prompt and "customers" in prompt and "vendors" in prompt


def test_classification_rejects_a_domain_outside_the_enum():
    """Enum-constrained output: the model can only ever name a domain from DOMAIN_NAMES --
    anything else fails Pydantic validation rather than being silently accepted downstream."""
    with pytest.raises(ValidationError):
        Classification(domains=["secrets"], confidence=0.9)


def test_classification_defaults_are_conservative():
    """An empty/default Classification should read as low-confidence and domain-less, so the
    graph's routing (confidence < threshold or domains empty -> clarify) fails safe."""
    c = Classification()
    assert c.domains == []
    assert c.confidence == 0.0
    assert c.clarification_question is None
