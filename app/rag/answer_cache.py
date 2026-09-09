"""TTL + LRU cache for repeated identical questions within one conversation.

Avoids repeating identical (and costly) Gemini + MongoDB round-trips when the same question is
asked again in the same channel within a bounded time window. Storage is `app/state`'s TtlStore,
so this is in-process by default and shared across replicas when STATE_BACKEND=redis -- the cache
was the least urgent of the guardrails to share (a miss costs latency, not correctness) but the
most expensive to lose, since every replica would otherwise re-render the same PDF.

Cache key: (channel_id, principal_scope, output_format, normalized_question).

Channel scoping alone was not enough once `/login` arrived. A vendor-authenticated question is
answered from *that identity's rows only* (app/security/roles.py forces the filter), so caching it
under the channel meant the next person to ask the same words in the same channel was served
another vendor's data -- a cross-tenant leak, not just a stale answer. `principal_scope`
(role + user id, see app/security/roles.py) puts each authenticated identity in its own cache
namespace. It carries the role as well as the id because two principals with the same id under
different roles are answered from different rows.

`output_format` is in the key because the same question asked as text and as a PDF are different
deliverables. Without it, "orders by status" cached as text would be replayed to someone who
asked for "orders by status as a csv", and they would get prose and no file.
"""

import base64
from dataclasses import dataclass

from app.config import settings
from app.state.store import TtlStore

#: (channel_id, principal_scope, output_format, normalized_question)
CacheKey = tuple[str, str, str, str]


@dataclass(frozen=True)
class CachedResult:
    """What's replayed on a cache hit -- including any generated file.

    The file bytes are cached alongside the text deliberately: regenerating a PDF is the single
    most expensive thing on the request path, and a cache that stored only the prose meant a
    repeated report request either silently lost its attachment or paid full render cost again.
    """

    answer: str
    error: str | None = None
    file_bytes: bytes | None = None
    file_type: str | None = None

    def to_json(self) -> dict:
        """Base64 for the file, because this has to survive JSON -- and, with a shared backend,
        a network hop and another process."""
        return {
            "answer": self.answer,
            "error": self.error,
            "file_type": self.file_type,
            "file_b64": base64.b64encode(self.file_bytes).decode("ascii")
            if self.file_bytes
            else None,
        }

    @classmethod
    def from_json(cls, payload: dict) -> "CachedResult":
        encoded = payload.get("file_b64")
        return cls(
            answer=payload.get("answer", ""),
            error=payload.get("error"),
            file_bytes=base64.b64decode(encoded) if encoded else None,
            file_type=payload.get("file_type"),
        )


class AnswerCache:
    def __init__(self, ttl_seconds: float, max_entries: int, backend=None) -> None:
        self._store = TtlStore(
            namespace="answers", ttl_seconds=ttl_seconds, max_entries=max_entries, backend=backend
        )

    @staticmethod
    def make_key(
        channel_id: str | None,
        question: str,
        *,
        principal_scope: str | None = None,
        output_format: str = "text",
    ) -> "CacheKey":
        """Build the cache key. See the module docstring for why scope and format are in it."""
        return (
            channel_id or "",
            principal_scope or "",
            output_format,
            question.strip().lower(),
        )

    def get(self, key: "CacheKey") -> CachedResult | None:
        """The cached result, or None on a miss or an expired entry."""
        payload = self._store.get_json(key)
        return CachedResult.from_json(payload) if payload is not None else None

    def set(self, key: "CacheKey", value: CachedResult) -> None:
        self._store.set_json(key, value.to_json())


answer_cache = AnswerCache(
    ttl_seconds=settings.answer_cache_ttl_seconds,
    max_entries=settings.answer_cache_max_entries,
)
