"""Opt-in prompt-drift detection against the real Gemini API -- NOT run in normal CI.

The rest of this suite stubs every LLM call so it stays fast, free, and deterministic; that
means it can verify the deterministic guardrails (scoping, validation, clamping -- see
tests/test_golden_questions.py) but can never catch the one thing that actually drifts over
time: does the live model still classify real questions into the domains a human would expect?

Run explicitly, e.g. before a model/prompt change ships:

    RUN_LIVE_LLM_TESTS=1 pytest tests/test_live_classifier.py -v

Requires a real GEMINI_API_KEY in the environment (see app/config.py). Skipped otherwise so a
normal `pytest` run never depends on network access, an API key, or nondeterministic model output.
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_LLM_TESTS") != "1",
    reason="opt-in: set RUN_LIVE_LLM_TESTS=1 (and a real GEMINI_API_KEY) to run this",
)


@pytest.fixture(scope="module")
def gemini():
    from app.llm.gemini_client import GeminiClient

    return GeminiClient()


@pytest.mark.parametrize(
    "question,expected_domain",
    [
        ("how many orders were placed last week?", "orders"),
        ("how many active vendors are there?", "vendors"),
        ("list active customers in Karachi", "customers"),
        ("show me vendors within 5km of customer USR-00001", "vendors"),
    ],
)
def test_classifier_names_the_expected_domain(gemini, question, expected_domain):
    from app.agents.classifier import classify_question

    result = classify_question(gemini, question)
    assert expected_domain in result.domains


def test_classifier_flags_geo_intent_for_a_nearby_question(gemini):
    from app.agents.classifier import classify_question

    result = classify_question(gemini, "vendors near me within 2km")
    assert result.needs_geo is True


def test_classifier_asks_for_clarification_on_a_vague_question(gemini):
    from app.agents.classifier import classify_question

    result = classify_question(gemini, "show me the active ones nearby")
    assert result.confidence < 0.55 or result.clarification_question is not None
