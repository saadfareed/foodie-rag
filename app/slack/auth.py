"""Vendor sign-in state for `/login`, used to scope queries to one vendor's own rows.

This is a **mock** authentication surface: `/login USR-00031` asserts an identity without
proving it, standing in for the OTP/SSO flow that would wrap it in a real deployment. It is
useful for demonstrating the scoping guardrail (app/agents/graph.py::_orders_id_filter forces
`vendor_id` onto every generated query) but it is not an access-control boundary on its own --
anyone who can invoke the command can claim any vendor id.

Sessions expire after VENDOR_SESSION_TTL_SECONDS. Without a TTL an abandoned session lives as
long as the process, so a shared machine or a long-running container keeps a stale identity
attached to a Slack user indefinitely -- and because the identity changes which rows a question
returns, a stale one silently changes answers.

Like every other stateful guardrail here, this is in-process only and does not survive a restart
or span replicas -- see the "Known limitations" section of CLAUDE.md.
"""

import time
from dataclasses import dataclass
from threading import Lock

from app.config import settings


@dataclass(frozen=True)
class _Session:
    vendor_id: str
    created_at: float


# Maps Slack user_id -> _Session
_vendor_auth_cache: dict[str, _Session] = {}
_auth_lock = Lock()


def login_vendor(slack_user_id: str, vendor_id: str) -> None:
    """Record a vendor session (a real SMS/email OTP flow would wrap this)."""
    with _auth_lock:
        _vendor_auth_cache[slack_user_id] = _Session(
            vendor_id=vendor_id, created_at=time.monotonic()
        )


def logout_vendor(slack_user_id: str | None) -> bool:
    """End a session. True if one was actually ended."""
    if not slack_user_id:
        return False
    with _auth_lock:
        return _vendor_auth_cache.pop(slack_user_id, None) is not None


def get_authenticated_vendor(slack_user_id: str | None) -> str | None:
    """The vendor_id this Slack user is signed in as, or None.

    An expired session is deleted on read rather than left for a sweep: this is the only place
    that observes expiry, so evicting here keeps the dict from growing without a background task.
    """
    if not slack_user_id:
        return None
    with _auth_lock:
        session = _vendor_auth_cache.get(slack_user_id)
        if session is None:
            return None
        ttl = settings.vendor_session_ttl_seconds
        if ttl > 0 and time.monotonic() - session.created_at > ttl:
            del _vendor_auth_cache[slack_user_id]
            return None
        return session.vendor_id


def clear_all() -> None:
    """Drop every session. For tests -- see the autouse fixtures in tests/conftest.py."""
    with _auth_lock:
        _vendor_auth_cache.clear()
