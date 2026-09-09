"""Who is asking, and what they are allowed to see.

Two credentials, deliberately different in kind:

* A **host-app key** (`WIDGET_API_KEYS`) is a long-lived server-side secret. It authenticates the
  host application's backend to `POST /v1/session` and nothing else. A browser must never hold
  one -- possession of it means the ability to mint a session for any user with any data scope.
* A **session token** is a short-lived signed JWT the browser does hold. It names the principal
  and the **role** the request will be answered under (`app/security/roles.py`), which is what
  decides which rows reach the answer.

The split is the whole security model. The browser cannot choose its own scope, because it never
holds the key that could mint one -- the host application's server decides who its logged-in user
is, and says so once, server to server. This is the web equivalent of `app/slack/auth.py`'s
`/login`, except the identity is actually proven rather than asserted.

Signing is HS256 with `WIDGET_JWT_SECRET`, which is separate from the host-app keys on purpose:
rotating a host key must not invalidate every live chat session, and leaking one must not confer
the ability to mint the other.
"""

import re
import secrets
import time
import uuid
from dataclasses import dataclass

import jwt

from app.config import settings
from app.messages import Failure
from app.security.roles import Principal, Role

#: Distinguishes a session token from a file-download token. Both are signed with the same key,
#: so without this a download URL would be accepted as proof of identity at /v1/ask (and vice
#: versa) -- a confused-deputy bug that costs one claim to close.
_SESSION_TYPE = "session"
_FILE_TYPE = "file"
#: The dev playground's sign-in cookie (see `app/api/server.py::dev_login`). A third type for the
#: same reason there is a second: the cookie is a *browser*-held credential that must not be
#: usable at /v1/ask, and a session token must not be usable as a sign-in cookie.
_DEV_TYPE = "dev"

_ALGORITHM = "HS256"


class TokenError(Exception):
    """A credential was missing, malformed, expired or not ours.

    Carries the `Failure` the user should be told about rather than a message of its own -- the
    text belongs to `app/messages.py`, like every other non-answer string in this codebase.
    """

    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.value)
        self.failure = failure


@dataclass(frozen=True)
class SessionClaims:
    """A verified browser session."""

    tenant_id: str
    principal_id: str
    role: Role
    #: What to call this person in the chat. Asserted by the host application when it mints the
    #: session, because it is the only party that knows -- the gateway holds an id and a role, and
    #: looking a name up per request would put a Mongo round-trip in front of every greeting.
    #: Optional throughout: a widget with no name greets without one rather than not at all.
    display_name: str | None = None

    @property
    def principal(self) -> Principal:
        """The identity the pipeline answers under.

        An admin is answered unfiltered, so its `user_id` is deliberately None -- there is nothing
        to scope it to, and carrying an id would suggest otherwise.
        """
        return Principal(
            role=self.role,
            user_id=None if self.role is Role.ADMIN else self.principal_id,
            tenant_id=self.tenant_id,
            display_name=self.display_name,
        )

    @property
    def conversation_id(self) -> str:
        """The value passed to the pipeline as `channel_id`.

        Derived from the token, never from the request body. A client-supplied conversation id
        would let one user attach to another's pending clarification, follow-up context and
        per-channel answer cache simply by guessing a string -- the browser equivalent of being
        able to type someone else's Slack channel into a message. Deriving it here makes that
        structurally impossible instead of a validation rule someone has to remember.

        One live conversation per principal, which mirrors a Slack DM. `reset` (see
        `app/rag/pipeline.py::_is_reset_command`) is how a user starts over.

        The role is part of it: the same person signed in as a customer and as an admin is
        answered from different rows, so they must not share a follow-up context either.
        """
        return f"web:{self.tenant_id}:{self.role.value}:{self.principal_id}"


#: A tenant id is a plain identifier. This is what disambiguates `tenant:secret` from a bare
#: secret that happens to contain a colon -- and a generated secret very often does. Splitting on
#: the first colon unconditionally silently reinterpreted such a key as a tenant plus a much
#: shorter secret, so the host application sent the whole string, the gateway compared it against
#: the tail, and every sign-in failed with a 401 that named nothing.
_TENANT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _api_key_map() -> dict[str, str]:
    """`{secret: tenant_id}` from `WIDGET_API_KEYS`.

    Entries are `tenant_id:secret`, or a bare `secret` for the single-tenant case (tenant
    `default`). The `tenant_id:` form is only recognised when the prefix is a plain identifier;
    anything else is treated as one whole secret, so a key containing punctuation is never
    silently truncated.

    Generate keys with `secrets.token_urlsafe(...)`, whose alphabet contains no colon, and the
    ambiguity cannot arise at all.

    Built per call rather than at import so a test can patch the setting the way every other test
    here does.
    """
    mapping: dict[str, str] = {}
    for entry in settings.widget_api_keys:
        tenant_id, separator, secret = entry.partition(":")
        if separator and secret and _TENANT_ID.match(tenant_id):
            mapping[secret] = tenant_id
        else:
            mapping[entry] = "default"
    return mapping


def _bearer(header_value: str | None) -> str:
    if not header_value:
        raise TokenError(Failure.NOT_AUTHENTICATED)
    scheme, separator, credential = header_value.partition(" ")
    if not separator or scheme.lower() != "bearer" or not credential.strip():
        raise TokenError(Failure.NOT_AUTHENTICATED)
    return credential.strip()


def authenticate_host_app(authorization: str | None) -> str:
    """The tenant id behind a host-app key, or raise.

    Compared with `compare_digest` rather than `==`: this endpoint is the one place an attacker
    can submit guesses against a long-lived secret, and a short-circuiting comparison leaks its
    prefix a byte at a time.
    """
    candidate = _bearer(authorization)
    for secret, tenant_id in _api_key_map().items():
        if secrets.compare_digest(candidate, secret):
            return tenant_id
    raise TokenError(Failure.NOT_AUTHENTICATED)


def mint_session_token(
    *, tenant_id: str, principal_id: str, role: Role, display_name: str | None = None
) -> tuple[str, int]:
    """A signed session token for one logged-in user, and its lifetime in seconds.

    `role` is what the host application asserts about this user; it reaches the agent graph as a
    `Principal` and is turned into a forced filter, in code, on every generated query. There is no
    default and no "unscoped" value -- the previous shape used `vendor_id=None` to mean "sees
    everything", which made a forgotten scope an admin session.
    """
    ttl = settings.widget_session_ttl_seconds
    now = int(time.time())
    payload = {
        "typ": _SESSION_TYPE,
        "tid": tenant_id,
        "sub": principal_id,
        "role": role.value,
        "iat": now,
        "exp": now + ttl,
    }
    if display_name:
        # Only when there is one. An empty claim would be indistinguishable from a name that is
        # genuinely blank, and the greeting has a perfectly good nameless form.
        payload["nm"] = display_name
    return jwt.encode(payload, settings.widget_jwt_secret, algorithm=_ALGORITHM), ttl


def verify_session_token(authorization: str | None) -> SessionClaims:
    """Verified claims, or raise `TokenError`.

    Expiry is reported separately from every other rejection: "reload the page" is a useful
    instruction and "you aren't signed in" is a confusing one when the user plainly is.
    """
    payload = _decode(_bearer(authorization), expected_type=_SESSION_TYPE)
    principal_id = payload.get("sub")
    if not isinstance(principal_id, str) or not principal_id:
        raise TokenError(Failure.NOT_AUTHENTICATED)
    try:
        role = Role(payload.get("role"))
    except ValueError:
        # An unrecognised role is a rejected token, never a defaulted one. A token minted by an
        # older or newer version of this service must not fall back to *any* role -- and the two
        # available fallbacks are wrong in opposite directions.
        raise TokenError(Failure.NOT_AUTHENTICATED) from None
    if role is Role.ANONYMOUS:
        raise TokenError(Failure.NOT_AUTHENTICATED)
    name = payload.get("nm")
    return SessionClaims(
        tenant_id=str(payload.get("tid") or "default"),
        principal_id=principal_id,
        role=role,
        display_name=name if isinstance(name, str) and name else None,
    )


def mint_file_token(*, claims: SessionClaims, file_id: str) -> str:
    """A one-file, one-principal download credential.

    A separate token rather than reusing the session one because it travels differently: a
    download URL ends up in the browser's address bar, history and any referrer -- places a
    session token has no business being. This one grants exactly one file and expires with it.
    """
    now = int(time.time())
    payload = {
        "typ": _FILE_TYPE,
        "tid": claims.tenant_id,
        "sub": claims.principal_id,
        "fid": file_id,
        "iat": now,
        "exp": now + settings.widget_file_ttl_seconds,
    }
    return jwt.encode(payload, settings.widget_jwt_secret, algorithm=_ALGORITHM)


def verify_file_token(token: str | None, *, file_id: str) -> SessionClaims:
    """Claims for a download, or raise. The token must name *this* file."""
    if not token:
        raise TokenError(Failure.NOT_AUTHENTICATED)
    payload = _decode(token, expected_type=_FILE_TYPE)
    if payload.get("fid") != file_id:
        raise TokenError(Failure.NOT_AUTHENTICATED)
    principal_id = payload.get("sub")
    if not isinstance(principal_id, str) or not principal_id:
        raise TokenError(Failure.NOT_AUTHENTICATED)
    # Role is irrelevant to a download: the file was already built under whatever role generated
    # it, and app/api/files.py checks it belongs to this principal. CUSTOMER is the least
    # privileged value that isn't ANONYMOUS, and nothing reads it.
    return SessionClaims(
        tenant_id=str(payload.get("tid") or "default"),
        principal_id=principal_id,
        role=Role.CUSTOMER,
    )


def mint_dev_login_cookie(*, tenant_id: str, principal: Principal) -> tuple[str, int]:
    """The playground's own sign-in cookie, and its lifetime.

    The playground now authenticates -- an email and a password checked against `users` -- so what
    it remembers afterwards has to be unforgeable. The previous cookie was a plain
    `role:user_id` string the page wrote itself, which was honest while the page was a role
    dropdown and would be a lie next to a password field: anyone reaching the endpoint could type
    `admin:` into their own cookie jar.

    Signed with the same key and carrying its own `typ`, so it is neither usable at /v1/ask nor
    interchangeable with a download URL. Longer-lived than a session token because it stands in
    for a host application's login cookie, not for a chat session: `/v1/dev/session` re-checks the
    account every time it mints from this.
    """
    ttl = max(settings.widget_session_ttl_seconds, 3600)
    now = int(time.time())
    payload = {
        "typ": _DEV_TYPE,
        "tid": tenant_id,
        "sub": principal.user_id or "",
        "role": principal.role.value,
        "nm": principal.display_name or "",
        "iat": now,
        "exp": now + ttl,
    }
    return jwt.encode(payload, settings.widget_jwt_secret, algorithm=_ALGORITHM), ttl


def verify_dev_login_cookie(raw: str | None) -> SessionClaims:
    """Claims for a signed-in playground user, or raise.

    Deliberately the same `SessionClaims` shape the rest of the gateway speaks, so
    `/v1/dev/session` mints from it through exactly the same checks a real session goes through.
    """
    if not raw:
        raise TokenError(Failure.NOT_AUTHENTICATED)
    payload = _decode(raw, expected_type=_DEV_TYPE)
    try:
        role = Role(payload.get("role"))
    except ValueError:
        raise TokenError(Failure.NOT_AUTHENTICATED) from None
    if role is Role.ANONYMOUS:
        raise TokenError(Failure.NOT_AUTHENTICATED)
    name = payload.get("nm")
    return SessionClaims(
        tenant_id=str(payload.get("tid") or "default"),
        # An admin asserted without an account legitimately has no id; every other role's id was
        # read out of `users` when the password was checked.
        principal_id=str(payload.get("sub") or ""),
        role=role,
        display_name=name if isinstance(name, str) and name else None,
    )


def new_file_id() -> str:
    """An unguessable id, so a download URL isn't enumerable even before its token is checked."""
    return uuid.uuid4().hex


def _decode(token: str, *, expected_type: str) -> dict:
    if not settings.widget_jwt_secret:
        # Defence in depth behind settings.widget_config_error(): PyJWT will happily verify
        # against an empty key, which would make every forged token valid.
        raise TokenError(Failure.NOT_AUTHENTICATED)
    try:
        payload = jwt.decode(
            token,
            settings.widget_jwt_secret,
            algorithms=[_ALGORITHM],
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError(Failure.SESSION_EXPIRED) from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError(Failure.NOT_AUTHENTICATED) from exc
    if payload.get("typ") != expected_type:
        raise TokenError(Failure.NOT_AUTHENTICATED)
    return payload
