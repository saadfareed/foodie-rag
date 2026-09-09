"""TTL cache remembering that we asked a clarifying question, so the user's
next message can be merged with the original one instead of being treated as unrelated.

Storage is `app/state`'s TtlStore: in-process by default, and shared across replicas when
STATE_BACKEND=redis -- without that, a follow-up answered by a different replica finds no pending
state and is treated as an unrelated question.

Keyed by (channel_id, user_id) rather than (channel_id, question) -- a clarification round-trip
is about one user's specific back-and-forth, not a question shareable across a channel like
app/rag/answer_cache.py's cached answers are.
"""

from dataclasses import dataclass

from app.config import settings
from app.state.store import TtlStore


@dataclass(frozen=True)
class PendingClarification:
    original_question: str
    rounds: int


class ClarificationCache:
    def __init__(self, ttl_seconds: float, max_entries: int, backend=None) -> None:
        self._store = TtlStore(
            namespace="clarifications",
            ttl_seconds=ttl_seconds,
            max_entries=max_entries,
            backend=backend,
        )

    @staticmethod
    def make_key(channel_id: str | None, user_id: str | None) -> tuple[str, str]:
        return (channel_id or "", user_id or "")

    def get(self, key: tuple[str, str]) -> PendingClarification | None:
        payload = self._store.get_json(key)
        if payload is None:
            return None
        return PendingClarification(
            original_question=payload.get("original_question", ""), rounds=payload.get("rounds", 1)
        )

    def set(self, key: tuple[str, str], value: PendingClarification) -> None:
        self._store.set_json(
            key, {"original_question": value.original_question, "rounds": value.rounds}
        )

    def clear(self, key: tuple[str, str]) -> None:
        self._store.delete(key)


clarification_cache = ClarificationCache(
    ttl_seconds=settings.clarification_cache_ttl_seconds,
    max_entries=500,
)
