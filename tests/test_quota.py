from datetime import datetime, timedelta, timezone

from app.llm.quota import QuotaTracker


class _FrozenClock:
    """Stands in for `datetime` so a test can move the calendar.

    Only `now()` is used by the module under test -- a minimal fake exposing exactly the method
    exercised, matching the _StubGemini/_FakeDb pattern used elsewhere in this suite.
    """

    def __init__(self, moment: datetime) -> None:
        self._moment = moment

    def now(self, tz=None) -> datetime:
        return self._moment


def test_unlimited_budget_never_over():
    tracker = QuotaTracker(daily_budget=0)
    for _ in range(1000):
        tracker.record_call()
    assert tracker.is_over_budget() is False
    assert tracker.remaining_budget() == -1


def test_budget_enforced_within_limit():
    tracker = QuotaTracker(daily_budget=2)
    assert tracker.remaining_budget() == 2
    assert tracker.is_over_budget() is False

    tracker.record_call()
    assert tracker.remaining_budget() == 1
    assert tracker.is_over_budget() is False

    tracker.record_call()
    assert tracker.remaining_budget() == 0
    assert tracker.is_over_budget() is True


def test_budget_resets_on_new_day(monkeypatch):
    """The counter is keyed by UTC date, so a new day is a new key rather than a reset anyone has
    to remember to perform."""
    tracker = QuotaTracker(daily_budget=1)
    tracker.record_call()
    assert tracker.is_over_budget() is True

    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    monkeypatch.setattr("app.llm.quota.datetime", _FrozenClock(tomorrow))

    assert tracker.is_over_budget() is False
    assert tracker.remaining_budget() == 1
