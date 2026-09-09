"""Vendor sign-in state for `/login`, used to scope queries to one vendor's own rows.

This is a **mock** authentication surface: `/login USR-00031` asserts an identity without proving
it, standing in for the OTP/SSO flow that would wrap it in a real deployment. It is useful for
demonstrating the scoping guardrail (`app/security/roles.py` forces `vendor_id`
onto every generated query) but it is not an access-control boundary on its own -- anyone who can
invoke the command can claim any vendor id. The web adapter's equivalent
(`app/api/tokens.py`) *is* a real one, because the host application's server proves the identity
before the gateway ever sees it.

Sessions expire after VENDOR_SESSION_TTL_SECONDS. Without a TTL an abandoned session lives as long
as the process, so a shared machine or a long-running container keeps a stale identity attached to
a Slack user indefinitely -- and because the identity changes which rows a question returns, a
stale one silently changes answers.

Storage is `app/state`'s TtlStore, so with STATE_BACKEND=redis a user stays signed in whichever
replica handles their next message. In-process, a `/login` followed by a question routed elsewhere
would silently answer unscoped -- returning *more* data than the user should see, which is the
worst direction for this particular piece of state to fail in.
"""

from app.config import settings
from app.security.roles import Principal, Role
from app.state.store import TtlStore

_store = TtlStore(
    namespace="vendor_sessions",
    ttl_seconds=settings.vendor_session_ttl_seconds,
    max_entries=1000,
)


def login_vendor(slack_user_id: str, vendor_id: str) -> None:
    """Record a vendor session (a real SMS/email OTP flow would wrap this)."""
    _store.set_json(slack_user_id, vendor_id)


def logout_vendor(slack_user_id: str | None) -> bool:
    """End a session. True if one was actually ended."""
    if not slack_user_id:
        return False
    existed = _store.get_json(slack_user_id) is not None
    _store.delete(slack_user_id)
    return existed


def get_authenticated_vendor(slack_user_id: str | None) -> str | None:
    """The vendor_id this Slack user is signed in as, or None.

    Expiry is the store's job now rather than something checked here -- an expired entry simply
    isn't returned, and nothing has to sweep.
    """
    if not slack_user_id:
        return None
    vendor_id = _store.get_json(slack_user_id)
    return vendor_id if isinstance(vendor_id, str) else None


def get_principal(slack_user_id: str | None) -> Principal:
    """The identity a Slack question is answered under.

    A `/login` session makes this a vendor principal. Without one, it is
    `SLACK_DEFAULT_ROLE` -- which defaults to `admin`, preserving the behaviour Slack users have
    always had, since a Slack channel is already gated by SLACK_ALLOWED_CHANNEL_IDS /
    SLACK_ALLOWED_USER_IDS and everyone in one was previously answered unfiltered.

    That default is the one place in this codebase where "not signed in" still means "sees
    everything", and it is deliberate rather than overlooked: silently changing it would break
    every existing deployment's Slack bot. Set SLACK_DEFAULT_ROLE=anonymous if the people in those
    channels are not all trusted as operators. The web adapter has no equivalent default -- a
    session token there must name its role.
    """
    vendor_id = get_authenticated_vendor(slack_user_id)
    if vendor_id:
        return Principal(role=Role.VENDOR, user_id=vendor_id)
    return Principal(role=Role(settings.slack_default_role))


def clear_all() -> None:
    """Drop every session. For tests -- see the autouse fixtures in tests/conftest.py."""
    _store.clear()
