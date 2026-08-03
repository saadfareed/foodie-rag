import pytest

from app.rag.answer_cache import AnswerCache


@pytest.fixture(autouse=True)
def _isolated_answer_cache(monkeypatch):
    """app.rag.pipeline.answer_cache is a module-level singleton; without this, tests that reuse
    the same question text (with the default channel_id=None) would leak cached answers into each
    other depending on test order."""
    monkeypatch.setattr(
        "app.rag.pipeline.answer_cache", AnswerCache(ttl_seconds=1800, max_entries=500)
    )
