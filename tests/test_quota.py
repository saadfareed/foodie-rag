from datetime import date, timedelta

from app.llm.quota import QuotaTracker


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


def test_budget_resets_on_new_day():
    tracker = QuotaTracker(daily_budget=1)
    tracker.record_call()
    assert tracker.is_over_budget() is True

    tracker._day = date.today() - timedelta(days=1)
    assert tracker.is_over_budget() is False
    assert tracker.remaining_budget() == 1
