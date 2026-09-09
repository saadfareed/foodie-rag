"""Short-lived home for a generated report between answering and downloading.

Slack handed report bytes straight to `files_upload_v2` and the request was over. A browser can't
receive bytes in a JSON reply it also wants to render as chat, so the answer carries a URL and the
bytes wait here for the fetch that follows a second later.

Four properties this has to hold, all of which are why it isn't a bare dict:

* **Bounded by time.** `WIDGET_FILE_TTL_SECONDS`. A URL that outlives its answer is an
  accumulating store of query results -- real customer rows -- that nobody is watching.
* **Bounded by count.** `WIDGET_MAX_CACHED_FILES`, LRU-evicted in the in-memory backend. Report
  bytes are megabytes, not the kilobytes the answer cache holds, so unbounded here is an
  out-of-memory bug rather than a slow leak.
* **Bound to a principal.** An entry records who it was generated for and is only returned to
  them. The download token already proves identity; this checks the file was actually theirs, so a
  token and an id belonging to two different people don't combine into one valid download.
* **Reachable from any replica.** With STATE_BACKEND=redis the bytes are in Redis, so a download
  works wherever it lands. In-process (the default), it only works on the replica that answered --
  which is exactly why running more than one instance needs the shared backend, not just a load
  balancer.

Metadata and bytes are two keys, not one JSON blob: base64-ing a multi-megabyte PDF to fit it
beside its own filename would inflate it by a third for no reason, and the metadata is the only
part that ever needs decoding to answer "is this yours?".
"""

from dataclasses import dataclass

from app.config import settings
from app.state.store import TtlStore

_NAMESPACE = "report_files"


@dataclass(frozen=True)
class StoredFile:
    tenant_id: str
    principal_id: str
    file_type: str
    filename: str
    content: bytes


class FileStore:
    #: Used when WIDGET_FILE_TTL_SECONDS is zero or negative.
    _MINIMUM_TTL_SECONDS = 60.0

    def __init__(self, ttl_seconds: float, max_entries: int, backend=None) -> None:
        # The state layer reads a non-positive TTL as "never expires" -- correct for a `/login`
        # session (VENDOR_SESSION_TTL_SECONDS=0 disables expiry by design) and exactly wrong here,
        # where it would turn a misconfiguration into a permanent store of customers' query
        # results. This is the one store where the safe reading of "0" is a short life, not none.
        if ttl_seconds <= 0:
            ttl_seconds = self._MINIMUM_TTL_SECONDS
        self._store = TtlStore(
            namespace=_NAMESPACE,
            ttl_seconds=ttl_seconds,
            # Doubled because each file is two keys (metadata + bytes), and the LRU bound counts
            # keys. Passing max_entries straight through would cap the store at half the number
            # of files the setting names.
            max_entries=max_entries * 2,
            backend=backend,
        )

    def put(self, file_id: str, stored: StoredFile) -> None:
        self._store.set_json(
            f"meta:{file_id}",
            {
                "tenant_id": stored.tenant_id,
                "principal_id": stored.principal_id,
                "file_type": stored.file_type,
                "filename": stored.filename,
            },
        )
        self._store.set_bytes(f"blob:{file_id}", stored.content)

    def get(self, file_id: str, *, tenant_id: str, principal_id: str) -> StoredFile | None:
        """The file, or None if it never existed, has expired, or belongs to someone else.

        The three cases are deliberately indistinguishable to the caller: telling a requester
        "that file exists but isn't yours" confirms an id they shouldn't have been able to confirm.
        """
        meta = self._store.get_json(f"meta:{file_id}")
        if meta is None:
            return None
        if meta.get("tenant_id") != tenant_id or meta.get("principal_id") != principal_id:
            return None
        content = self._store.get_bytes(f"blob:{file_id}")
        if content is None:
            # Metadata outliving its bytes means the LRU evicted one key and not the other. A
            # half-present file is a miss, not a zero-byte download.
            return None
        return StoredFile(
            tenant_id=meta["tenant_id"],
            principal_id=meta["principal_id"],
            file_type=meta.get("file_type", ""),
            filename=meta.get("filename", "report"),
            content=content,
        )

    def clear(self) -> None:
        """Drop every stored file. For tests -- see the autouse fixtures in tests/conftest.py."""
        self._store.clear()


file_store = FileStore(
    ttl_seconds=settings.widget_file_ttl_seconds,
    max_entries=settings.widget_max_cached_files,
)
