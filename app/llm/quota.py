"""In-process daily call budget for Gemini requests.

Resets on process restart and isn't shared across instances -- acceptable for the
current single-process Socket Mode deployment. Revisit with a shared store (Mongo/Redis)
if this ever runs as multiple instances.
"""

import threading
from datetime import date

from app.config import settings


class QuotaTracker:
    def __init__(self, daily_budget: int) -> None:
        self._daily_budget = daily_budget
        self._lock = threading.Lock()
        self._day = date.today()
        self._calls_today = 0

    def _reset_if_new_day(self) -> None:
        today = date.today()
        if today != self._day:
            self._day = today
            self._calls_today = 0

    def record_call(self) -> None:
        with self._lock:
            self._reset_if_new_day()
            self._calls_today += 1

    def remaining_budget(self) -> int:
        with self._lock:
            self._reset_if_new_day()
            if self._daily_budget <= 0:
                return -1  # unlimited
            return max(self._daily_budget - self._calls_today, 0)

    def is_over_budget(self) -> bool:
        with self._lock:
            self._reset_if_new_day()
            if self._daily_budget <= 0:
                return False
            return self._calls_today >= self._daily_budget


quota_tracker = QuotaTracker(settings.gemini_daily_call_budget)
