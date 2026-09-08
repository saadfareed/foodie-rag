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
    expected = Classification(
        domains=["vendors"],
        needs_geo=True,
        confidence=0.8,
        resolved_question="active vendors nearby",
    )
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
    assert c.context_mode == "new_topic"


def test_classification_rejects_a_context_mode_outside_the_enum():
    with pytest.raises(ValidationError):
        Classification(domains=["orders"], confidence=0.9, context_mode="remembers_everything")


def test_classify_question_backfills_resolved_question_when_the_model_leaves_it_blank():
    """A Classification with resolved_question left at its "" default (e.g. a stub/model that
    doesn't set it) must not leak an empty string to callers -- it should read as the question
    that was actually classified."""
    gemini = _StubGemini(Classification(domains=["orders"], confidence=0.9))

    result = classify_question(gemini, "how many pending orders")

    assert result.resolved_question == "how many pending orders"


def test_classify_question_omits_previous_question_section_when_none():
    gemini = _StubGemini(Classification(domains=["orders"], confidence=0.9))

    classify_question(gemini, "how many pending orders")

    # "Previous question in this conversation" is the dynamic section's exact wording -- distinct
    # from the static few-shot examples in the prompt template, which say "Previous question:"
    # (as a bullet) and always appear regardless of whether previous_question was passed.
    assert "Previous question in this conversation" not in gemini.prompts[0]


def test_classify_question_includes_previous_question_when_given():
    gemini = _StubGemini(
        Classification(
            domains=["orders"],
            confidence=0.9,
            context_mode="followup",
            resolved_question="what is the grand total of orders vendor V1 had last week?",
        )
    )

    result = classify_question(
        gemini,
        "what about the grand total?",
        previous_question="how many orders did vendor V1 have last week?",
    )

    prompt = gemini.prompts[0]
    assert (
        "Previous question in this conversation (may or may not be related): "
        '"how many orders did vendor V1 have last week?"'
    ) in prompt
    assert prompt.rstrip().endswith("Question: what about the grand total?")
    assert result.context_mode == "followup"
    assert result.resolved_question == "what is the grand total of orders vendor V1 had last week?"


# --- output_format / report_title ----------------------------------------------------------
#
# Both ride on this call rather than a dedicated one. A separate "classify the intent" request
# cost a full quota unit per question -- against a free tier measured in tens -- to return a
# single word, so these fields are what make the format decision free.


def test_output_format_defaults_to_text():
    """A Classification built without mentioning format -- every pre-existing call site, and any
    model response omitting the field -- must read as a plain text answer."""
    assert Classification().output_format == "text"


def test_report_title_defaults_to_empty():
    """Empty means "no opinion"; the caller falls back to REPORT_TITLE."""
    assert Classification().report_title == ""


@pytest.mark.parametrize("output_format", ["text", "csv", "xlsx", "pdf"])
def test_every_supported_format_is_accepted(output_format):
    assert Classification(output_format=output_format).output_format == output_format


def test_an_unknown_output_format_is_rejected_structurally():
    """Enum-constrained like `domains`: a hallucinated format fails Pydantic validation rather
    than reaching the report layer and being silently ignored."""
    with pytest.raises(ValidationError):
        Classification(output_format="powerpoint")


def test_classify_question_passes_the_format_and_title_through():
    expected = Classification(
        domains=["orders"],
        confidence=0.9,
        output_format="csv",
        report_title="Last 10 Incomplete Order Details",
    )
    gemini = _StubGemini(expected)

    result = classify_question(gemini, "I need last 10 incomplete order details in csv")

    assert result.output_format == "csv"
    assert result.report_title == "Last 10 Incomplete Order Details"


def test_the_prompt_asks_for_both_new_fields():
    """The model can only fill in a field the prompt actually describes."""
    gemini = _StubGemini(Classification(domains=["orders"], confidence=0.9))

    classify_question(gemini, "how many orders?")

    prompt = gemini.prompts[0]
    assert "output_format" in prompt
    assert "report_title" in prompt
