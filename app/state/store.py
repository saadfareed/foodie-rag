"""A TTL-bounded keyed store, shared by every guardrail that needs one.

Before this existed, `answer_cache.py`, `clarification_cache.py`, `conversation_context.py` and
`context_switch_cache.py` each carried their own copy of the same forty lines -- an
`OrderedDict[key, (timestamp, value)]`, a lock, expire-on-read, LRU eviction -- and each said
"same shape as" the others in its docstring. Four copies of one algorithm is tolerable while it
only ever runs in one process; it stops being tolerable the moment that algorithm also has to
exist in Redis, because then it is four chances to get distributed expiry subtly different.

So the algorithm lives here once, over `app/state`'s backend primitives, and each cache keeps
what is actually specific to it: its namespace, its TTL, and how its own value type is encoded.

Keys arrive as tuples (`(channel_id, user_id)`, and the answer cache's four-part key) because
that is what the call sites already used and what reads clearly. They are joined with a unit
separator -- a byte no channel id, user id or question text will contain -- and hashed once they
get long, so an entire question doesn't end up in a Redis key name.
"""

import hashlib
import json
from typing import Any

from app.state import get_backend

#: ASCII unit separator. Not a character a Slack id, a principal id or free-text can contain,
#: so ("a", "b:c") and ("a:b", "c") cannot collide into one key.
_SEPARATOR = "\x1f"

#: Past this, the joined key is hashed instead. Short keys stay readable in `redis-cli`; long
#: ones (the answer cache embeds the whole question) don't bloat the keyspace.
_MAX_READABLE_KEY = 160


def encode_key(parts: tuple[str, ...] | str) -> str:
    if isinstance(parts, str):
        joined = parts
    else:
        joined = _SEPARATOR.join(parts)
    if len(joined) <= _MAX_READABLE_KEY:
        return joined
    return "h:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()


class TtlStore:
    """TTL + (in-memory) LRU storage for one guardrail's namespace."""

    def __init__(self, namespace: str, ttl_seconds: float, max_entries: int, backend=None) -> None:
        self._namespace = namespace
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        # Resolved per call rather than captured here: these stores are module-level singletons
        # built at import time, and a test that swaps the backend afterwards must be seen by all
        # of them (see tests/conftest.py). Capturing a backend at construction would pin every
        # cache to whichever one happened to exist when the module was first imported.
        self._backend = backend

    def _resolve(self):
        return self._backend if self._backend is not None else get_backend()

    def get_json(self, key: tuple[str, ...] | str) -> Any | None:
        raw = self._resolve().get(self._namespace, encode_key(key))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            # A value we can't read is a value we don't have. Shared state outlives a deploy, so
            # an older or newer replica's encoding shows up here as a miss -- which costs one
            # recomputation -- rather than as an exception on the request path.
            return None

    def set_json(self, key: tuple[str, ...] | str, value: Any) -> None:
        self._resolve().set(
            self._namespace,
            encode_key(key),
            json.dumps(value).encode("utf-8"),
            ttl_seconds=self._ttl_seconds,
            max_entries=self._max_entries,
        )

    def get_bytes(self, key: tuple[str, ...] | str) -> bytes | None:
        return self._resolve().get(self._namespace, encode_key(key))

    def set_bytes(self, key: tuple[str, ...] | str, value: bytes) -> None:
        self._resolve().set(
            self._namespace,
            encode_key(key),
            value,
            ttl_seconds=self._ttl_seconds,
            max_entries=self._max_entries,
        )

    def delete(self, key: tuple[str, ...] | str) -> None:
        self._resolve().delete(self._namespace, encode_key(key))

    def clear(self) -> None:
        """Drop this namespace only -- see StateBackend.clear_namespace."""
        self._resolve().clear_namespace(self._namespace)
