"""In-process TTL + LRU cache remembering the last *resolved* question asked in a
(channel, user) conversation, so a short elliptical follow-up ("total amount?" after "how many
orders?") can be answered with the right context -- without carrying a growing chat history
forward on every turn.

Same shape as app/rag/clarification_cache.py (OrderedDict + threading.Lock + TTL) and the same
documented limitation as that module: in-process, single-replica only -- acceptable for the
current Socket Mode deployment, revisit with a shared store (Mongo/Redis) if this ever runs as
multiple instances.

Deliberately stores only ONE resolved question per (channel, user), not a growing transcript:
app/agents/classifier.py folds whatever context a follow-up needs into a single rewritten
`resolved_question` each turn, so there's never more than one turn's text to carry forward. An
unrelated question (Classification.context_mode == "new_topic") never touches this cache at
all on the way in -- only overwrites it on the way out -- so the common case pays zero extra
prompt tokens.

Keyed by (channel_id, user_id) rather than (channel_id, question), matching
app/rag/clarification_cache.py -- this is about one user's specific conversational thread, not
a question shareable across a channel like app/rag/answer_cache.py's cached answers are.
"""

import threading
import time
from collections import OrderedDict

from app.config import settings


class ConversationContextCache:
    def __init__(self, ttl_seconds: float, max_entries: int) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple[str, str], tuple[float, str]] = OrderedDict()

    @staticmethod
    def make_key(channel_id: str | None, user_id: str | None) -> tuple[str, str]:
        return (channel_id or "", user_id or "")

    def get(self, key: tuple[str, str]) -> str | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            inserted_at, resolved_question = entry
            if time.monotonic() - inserted_at > self._ttl_seconds:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return resolved_question

    def set(self, key: tuple[str, str], resolved_question: str) -> None:
        with self._lock:
            self._entries[key] = (time.monotonic(), resolved_question)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._entries.pop(key, None)


conversation_context_cache = ConversationContextCache(
    ttl_seconds=settings.conversation_context_ttl_seconds,
    max_entries=500,
)
