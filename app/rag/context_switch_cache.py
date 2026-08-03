"""In-process TTL + LRU cache holding a not-yet-confirmed context switch.

When `app/agents/classifier.py` decides a message doesn't fit the still-live conversation
(`Classification.context_mode == "new_topic"` while `app/rag/conversation_context.py` has a
recent previous question), `app/agents/graph.py` doesn't silently discard that context or answer
the new message -- it asks the user to confirm first. The *candidate* question waits here until
the user replies; no Gemini call is spent generating a real answer for a question that might get
abandoned by a "no".

Same TTL+LRU shape as `app/rag/clarification_cache.py` and the same documented limitation:
in-process, single-replica only.

Keyed by (channel_id, user_id), like every other per-conversation cache in this package -- this
is one user's specific back-and-forth, not something shareable across a channel.
"""

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from app.config import settings


@dataclass(frozen=True)
class PendingContextSwitch:
    candidate_question: str


class ContextSwitchCache:
    def __init__(self, ttl_seconds: float, max_entries: int) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple[str, str], tuple[float, PendingContextSwitch]] = (
            OrderedDict()
        )

    @staticmethod
    def make_key(channel_id: str | None, user_id: str | None) -> tuple[str, str]:
        return (channel_id or "", user_id or "")

    def get(self, key: tuple[str, str]) -> PendingContextSwitch | None:
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

    def set(self, key: tuple[str, str], value: PendingContextSwitch) -> None:
        with self._lock:
            self._entries[key] = (time.monotonic(), value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._entries.pop(key, None)


context_switch_cache = ContextSwitchCache(
    ttl_seconds=settings.context_switch_confirmation_ttl_seconds,
    max_entries=500,
)
