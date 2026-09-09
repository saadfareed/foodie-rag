"""TTL cache remembering the last *resolved* question asked in a
(channel, user) conversation, so a short elliptical follow-up ("total amount?" after "how many
orders?") can be answered with the right context -- without carrying a growing chat history
forward on every turn.

Storage is `app/state`'s TtlStore: in-process by default, and shared across replicas when
STATE_BACKEND=redis -- without that, a follow-up answered by a different replica finds no pending
state and is treated as an unrelated question.

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

from app.config import settings
from app.state.store import TtlStore


class ConversationContextCache:
    def __init__(self, ttl_seconds: float, max_entries: int, backend=None) -> None:
        self._store = TtlStore(
            namespace="conversation",
            ttl_seconds=ttl_seconds,
            max_entries=max_entries,
            backend=backend,
        )

    @staticmethod
    def make_key(channel_id: str | None, user_id: str | None) -> tuple[str, str]:
        return (channel_id or "", user_id or "")

    def get(self, key: tuple[str, str]) -> str | None:
        return self._store.get_json(key)

    def set(self, key: tuple[str, str], resolved_question: str) -> None:
        self._store.set_json(key, resolved_question)

    def clear(self, key: tuple[str, str]) -> None:
        self._store.delete(key)


conversation_context_cache = ConversationContextCache(
    ttl_seconds=settings.conversation_context_ttl_seconds,
    max_entries=500,
)
