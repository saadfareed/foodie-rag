"""In-process TTL + LRU cache remembering that we asked a clarifying question, so the user's
next message can be merged with the original one instead of being treated as unrelated.

Same shape as app/rag/answer_cache.py (OrderedDict + threading.Lock + TTL) and the same
documented limitation as that module and app/llm/quota.py: in-process, single-replica only --
acceptable for the current Socket Mode deployment, revisit with a shared store (Mongo/Redis) if
this ever runs as multiple instances.

Keyed by (channel_id, user_id) rather than (channel_id, question) -- a clarification round-trip
is about one user's specific back-and-forth, not a question shareable across a channel like
app/rag/answer_cache.py's cached answers are.
"""

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from app.config import settings


@dataclass(frozen=True)
class PendingClarification:
    original_question: str
    rounds: int


class ClarificationCache:
    def __init__(self, ttl_seconds: float, max_entries: int) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple[str, str], tuple[float, PendingClarification]] = (
            OrderedDict()
        )

    @staticmethod
    def make_key(channel_id: str | None, user_id: str | None) -> tuple[str, str]:
        return (channel_id or "", user_id or "")

    def get(self, key: tuple[str, str]) -> PendingClarification | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            inserted_at, value = entry
            if time.monotonic() - inserted_at > self._ttl_seconds:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return value

    def set(self, key: tuple[str, str], value: PendingClarification) -> None:
        with self._lock:
            self._entries[key] = (time.monotonic(), value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._entries.pop(key, None)


clarification_cache = ClarificationCache(
    ttl_seconds=settings.clarification_cache_ttl_seconds,
    max_entries=500,
)
