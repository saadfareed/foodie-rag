"""TTL cache holding a not-yet-confirmed context switch.

When `app/agents/classifier.py` decides a message doesn't fit the still-live conversation
(`Classification.context_mode == "new_topic"` while `app/rag/conversation_context.py` has a
recent previous question), `app/agents/graph.py` doesn't silently discard that context or answer
the new message -- it asks the user to confirm first. The *candidate* question waits here until
the user replies; no Gemini call is spent generating a real answer for a question that might get
abandoned by a "no".

Storage is `app/state`'s TtlStore: in-process by default, and shared across replicas when
STATE_BACKEND=redis -- without that, a follow-up answered by a different replica finds no pending
state and is treated as an unrelated question.

Keyed by (channel_id, user_id), like every other per-conversation cache in this package -- this
is one user's specific back-and-forth, not something shareable across a channel.
"""

from dataclasses import dataclass

from app.config import settings
from app.state.store import TtlStore


@dataclass(frozen=True)
class PendingContextSwitch:
    candidate_question: str


class ContextSwitchCache:
    def __init__(self, ttl_seconds: float, max_entries: int, backend=None) -> None:
        self._store = TtlStore(
            namespace="context_switch",
            ttl_seconds=ttl_seconds,
            max_entries=max_entries,
            backend=backend,
        )

    @staticmethod
    def make_key(channel_id: str | None, user_id: str | None) -> tuple[str, str]:
        return (channel_id or "", user_id or "")

    def get(self, key: tuple[str, str]) -> PendingContextSwitch | None:
        payload = self._store.get_json(key)
        if payload is None:
            return None
        return PendingContextSwitch(candidate_question=payload.get("candidate_question", ""))

    def set(self, key: tuple[str, str], value: PendingContextSwitch) -> None:
        self._store.set_json(key, {"candidate_question": value.candidate_question})

    def clear(self, key: tuple[str, str]) -> None:
        self._store.delete(key)


context_switch_cache = ContextSwitchCache(
    ttl_seconds=settings.context_switch_confirmation_ttl_seconds,
    max_entries=500,
)
