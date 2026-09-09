"""The HTTP surface of the web chat plugin.

Structurally the mirror of `app/slack/handlers.py`: authorize, call `answer_question`, deliver.
Everything specific to *browsers* is here and nowhere else -- token verification, CORS, the
download endpoint, the widget script -- so the pipeline below stays a single implementation
serving both adapters rather than two that drift.

Status codes follow one rule, because the two kinds of outcome are genuinely different:

* A **gateway** rejection (no token, expired token, empty or oversized message) is an HTTP error
  -- 401/400 -- because the request never became a question.
* A **pipeline** outcome (rate limited, over budget, upstream down, no data) is HTTP 200 with
  `error` set, because it *is* the answer. The bot saying "you're asking faster than I can keep
  up" in chat is not a transport failure, and rendering it as one would put a stack-shaped error
  in front of a user who just needs to wait a minute.

Every reply body carries `text` regardless, so the widget has something to render in both cases.
"""

import json
import logging
import pathlib
import re
import threading
from typing import Annotated

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.middleware.cors import CORSMiddleware

from app.api.files import StoredFile, file_store
from app.api.tokens import (
    SessionClaims,
    TokenError,
    authenticate_host_app,
    mint_dev_login_cookie,
    mint_file_token,
    mint_session_token,
    new_file_id,
    verify_dev_login_cookie,
    verify_file_token,
    verify_session_token,
)
from app.config import settings
from app.db.accounts import AccountError, create_account, validate_signup
from app.db.identity import (
    AccountNotActiveError,
    authenticate_password,
    find_principal_by_email,
    principal_exists,
)
from app.db.mongo import get_db
from app.llm.gemini_client import GeminiClient
from app.messages import (
    Failure,
    account_creation_unavailable_message,
    account_not_active_message,
    chat_welcome,
    failure_message,
    invalid_credentials_message,
    new_reference,
    question_required_message,
    question_too_long_message,
    role_not_permitted_message,
    unknown_role_message,
)
from app.rag.pipeline import AnswerResult, answer_question
from app.rag.rate_limiter import rate_limiter
from app.rag.stream import QueueSink, StreamEvent
from app.security.roles import Principal, Role

logger = logging.getLogger("audit")

_STATIC_DIR = __file__.rsplit("/", 1)[0] + "/static"

#: Who may use the dev playground. Not a configurable list: the point is "this machine", and a
#: setting inviting someone to widen it would defeat the guard.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

#: The playground's sign-in cookie. HttpOnly and signed (app/api/tokens.py::mint_dev_login_cookie)
#: -- it names a role, and the page now has a password field in front of it.
_DEV_COOKIE = "dev_session"

#: Sections of the landing page that only an admin sees. Stripped from the HTML server-side rather
#: than hidden with CSS: what the gateway is and how to embed it is operator documentation, and a
#: page that ships it to everyone and then hides it is a preference, not a rule.
_ADMIN_ONLY_BLOCK = re.compile(r"<!--ADMIN-->.*?<!--/ADMIN-->", re.DOTALL)

#: Browsers decide what to do with a download by content type; getting this wrong turns a
#: spreadsheet into a text file the user has to rename.
_MEDIA_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
}


class SessionRequest(BaseModel):
    """What a host application's *server* sends to mint a session for its logged-in user."""

    user_id: str = Field(min_length=1, max_length=200)
    #: The role this user holds, asserted by the host app after it has verified who they are.
    #: Becomes a `Principal` (app/security/roles.py) and is turned into a forced filter, in code,
    #: on every generated query. Required -- there is deliberately no default, because both
    #: possible defaults are wrong: `admin` grants everything on a typo, and `customer` silently
    #: mis-scopes a vendor.
    role: str = Field(min_length=1, max_length=32)
    #: What to call this user in the chat. Optional: the gateway greets without a name rather
    #: than not at all, and looking one up per session would put a Mongo round-trip on a path
    #: that already knows the answer -- the host application just authenticated this person.
    name: str | None = Field(default=None, max_length=200)


class LoginRequest(BaseModel):
    """A playground sign-in. Both fields are checked together or not at all -- see
    `app/db/identity.py::authenticate_password`."""

    email: str = Field(default="", max_length=320)
    #: Bounded like every other browser-supplied string. The cap is far above any real password
    #: and far below anything worth spending a key derivation on.
    password: str = Field(default="", max_length=200)


class SignupRequest(BaseModel):
    """A new playground account. `role` is the *account type* being created, which is a different
    thing from a browser asserting a role at question time -- this one is written to `users` and
    then read back out of it on every subsequent sign-in, and it is bounded by
    `WIDGET_ALLOWED_SESSION_ROLES` so nobody can create an account the gateway then refuses to
    answer for."""

    name: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=320)
    password: str = Field(default="", max_length=200)
    role: str = Field(default="", max_length=32)
    city: str = Field(default="", max_length=120)
    #: Optional, and only ever what the browser's geolocation API offered. Stored as a GeoJSON
    #: Point so "vendors near me" uses the same 2dsphere index as every seeded row.
    latitude: float | None = None
    longitude: float | None = None
    business_name: str = Field(default="", max_length=200)
    category: str = Field(default="", max_length=80)


class IdentityLookupRequest(BaseModel):
    """A verified email address, exchanged for the identity it belongs to."""

    email: str = Field(min_length=3, max_length=320)


class AskRequest(BaseModel):
    question: str = Field(default="")


def _error(failure: Failure, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"text": failure_message(failure), "error": failure.value, "file": None},
    )


def _message(text: str, *, error: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"text": text, "error": error, "file": None}
    )


def _file_payload(result: AnswerResult, claims: SessionClaims) -> dict | None:
    """Park the report bytes and describe where to fetch them, or None if there's no file.

    The URL embeds a token minted for this principal and this file only -- see
    `app/api/tokens.py::mint_file_token` for why it isn't the session token.
    """
    if not result.has_file:
        return None
    file_id = new_file_id()
    filename = f"report.{result.file_type}"
    file_store.put(
        file_id,
        StoredFile(
            tenant_id=claims.tenant_id,
            principal_id=claims.principal_id,
            file_type=result.file_type or "",
            filename=filename,
            content=result.file_bytes or b"",
        ),
    )
    token = mint_file_token(claims=claims, file_id=file_id)
    return {
        "id": file_id,
        "type": result.file_type,
        "filename": filename,
        "url": f"/v1/files/{file_id}?t={token}",
    }


def _validated_question(payload_question: str) -> tuple[str | None, Response | None]:
    """The question to ask, or the response explaining why there isn't one.

    Shared by both ask endpoints. Two copies of "is this empty, is this too long" is two chances
    for the streaming endpoint to quietly accept what the plain one rejects.
    """
    question = payload_question.strip()
    if not question:
        return None, _message(question_required_message(), error="empty_question", status_code=400)
    if len(question) > settings.widget_max_question_chars:
        return None, _message(
            question_too_long_message(settings.widget_max_question_chars),
            error="question_too_long",
            status_code=400,
        )
    return question, None


def _point(latitude: float | None, longitude: float | None) -> dict | None:
    """A GeoJSON Point for a browser-supplied position, or None.

    Out-of-range values become None rather than an error: a location is a convenience on a sign-up
    form, and refusing the whole registration because a browser reported a bad fix would trade
    something that matters for something that doesn't. GeoJSON order is **[lng, lat]**, which is
    the reverse of how every geolocation API hands them over and the reason this is a function
    rather than a dict literal at the call site.
    """
    if latitude is None or longitude is None:
        return None
    if not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
        return None
    return {"type": "Point", "coordinates": [round(longitude, 6), round(latitude, 6)]}


def _sse_frame(event: StreamEvent) -> str:
    payload = {"text": event.text, **event.data}
    return f"event: {event.kind}\ndata: {json.dumps(payload)}\n\n"


def create_app(gemini: GeminiClient | None = None) -> FastAPI:
    """Build the gateway.

    `gemini` is the single shared client built once at startup (see `app/api/main.py`), injected
    the same way `register_handlers` takes it -- a per-request client would pay connection setup
    on every question and defeat the singleton the rest of the app relies on.
    """
    config_error = settings.widget_config_error()
    if config_error:
        # Refusing to boot is the only failure mode that surfaces this: a gateway missing its
        # signing key or its host-app keys starts fine and serves requests with no identity
        # checking at all, which looks exactly like working.
        raise RuntimeError(config_error)

    cors_warning = settings.widget_cors_warning()
    if cors_warning:
        logger.warning("widget_cors_warning", extra={"event": {"message": cors_warning}})

    # Said here as well as in app/main.py: the gateway builds its own GeminiClient, and a
    # deployment running only the web adapter would otherwise never hear it.
    retry_warning = settings.retry_budget_warning()
    if retry_warning:
        logger.warning("startup_config_warning", extra={"event": {"message": retry_warning}})

    timeout_warning = settings.gemini_timeout_warning()
    if timeout_warning:
        logger.warning("startup_config_warning", extra={"event": {"message": timeout_warning}})

    playground_warning = settings.dev_playground_warning()
    if playground_warning:
        logger.warning(
            "widget_dev_playground_enabled", extra={"event": {"message": playground_warning}}
        )

    app = FastAPI(title="Data chat gateway", docs_url=None, redoc_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.widget_allowed_origins or ["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        # Bearer tokens, never cookies -- so the browser must not be asked to send ambient
        # credentials, and `allow_origins=["*"]` stays legal rather than being silently ignored.
        allow_credentials=False,
    )

    client = gemini if gemini is not None else GeminiClient()

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    def _issue_session(
        tenant_id: str, user_id: str, role: Role, display_name: str | None = None
    ) -> Response:
        """Mint and log. Shared by /v1/session and the dev playground so the two cannot drift --
        the playground is meant to exercise the *real* path, not a parallel one that happens to
        look similar."""
        token, ttl = mint_session_token(
            tenant_id=tenant_id, principal_id=user_id, role=role, display_name=display_name
        )
        logger.info(
            "widget_session_issued",
            extra={"event": {"tenant_id": tenant_id, "user_id": user_id, "role": role.value}},
        )
        return JSONResponse({"token": token, "expires_in": ttl})

    def _check_session_request(tenant_id: str, user_id: str, role_name: str) -> Response | None:
        """The role and identity checks both session endpoints must pass, or the refusal."""
        try:
            role = Role(role_name.strip().lower())
        except ValueError:
            return _message(unknown_role_message(), error="unknown_role", status_code=400)
        if role is Role.ANONYMOUS or role.value not in settings.widget_allowed_session_roles:
            logger.warning(
                "widget_session_role_refused",
                extra={"event": {"tenant_id": tenant_id, "requested_role": role.value}},
            )
            return _message(
                role_not_permitted_message(), error="role_not_permitted", status_code=403
            )
        if (
            settings.widget_verify_asserted_identity
            and role is not Role.ADMIN
            and not principal_exists(get_db(), user_id, role=role)
        ):
            logger.warning(
                "widget_session_identity_refused",
                extra={"event": {"tenant_id": tenant_id, "role": role.value}},
            )
            return _message(
                role_not_permitted_message(), error="identity_not_active", status_code=403
            )
        return None

    @app.post("/v1/session")
    def create_session(
        payload: SessionRequest, authorization: Annotated[str | None, Header()] = None
    ) -> Response:
        """Server-to-server. The host app proves itself with its key and asserts who its user is.

        This is the only endpoint that decides scope, and it is deliberately unreachable from a
        browser: the credential it requires is one no browser should ever hold.
        """
        try:
            tenant_id = authenticate_host_app(authorization)
        except TokenError as exc:
            return _error(exc.failure, 401)

        # A host-app key is a long-lived secret on someone else's server. Restricting which roles
        # it may assert means a leaked key cannot mint an admin session unless an operator
        # deliberately allowed that -- `admin` is off by default for exactly that reason. This is
        # the one privilege-escalation path the gateway actually owns.
        #
        # The identity check that follows it is what makes the session TTL a revocation mechanism:
        # an admin has no row in `users` by design, but a vendor or customer must still be a live
        # account. A suspended account and one that never existed get the same refusal, because
        # distinguishing them tells a caller something about accounts it has not authenticated.
        refusal = _check_session_request(tenant_id, payload.user_id, payload.role)
        if refusal is not None:
            return refusal

        return _issue_session(
            tenant_id, payload.user_id, Role(payload.role.strip().lower()), payload.name
        )

    @app.post("/v1/identity/lookup")
    def identity_lookup(
        payload: IdentityLookupRequest, authorization: Annotated[str | None, Header()] = None
    ) -> Response:
        """Exchange an email address the host app has *already verified* for the identity it maps
        to, so the host app can mint a session without needing its own MongoDB credential.

        Server-to-server, host-app key only. This exists because the contact details live in the
        `users` collection, which only this service reads (and which
        `app/security/field_policy.py` denies to the model entirely) -- handing every host
        application a Mongo credential to do this lookup itself would be a far worse trade.

        **It does not verify anything.** Proving control of the address is the host app's job;
        this answers "who is that, and what may they see". Rate-limited per key, and a miss and a
        suspended account are the same response -- see app/db/identity.py.
        """
        try:
            tenant_id = authenticate_host_app(authorization)
        except TokenError as exc:
            return _error(exc.failure, 401)

        # Bounded independently of the per-user question limit: this endpoint is reachable by
        # anything holding a host key, and it is the one place an address can be tested for
        # existence. A flat ceiling per key is what stops it becoming an enumeration oracle even
        # for a legitimate but compromised host.
        if not rate_limiter.allow(("identity_lookup", tenant_id)):
            return _error(Failure.USER_RATE_LIMITED, 429)

        principal = find_principal_by_email(get_db(), payload.email, tenant_id=tenant_id)
        if principal is None:
            logger.info("identity_lookup_miss", extra={"event": {"tenant_id": tenant_id}})
            return JSONResponse({"found": False, "user_id": None, "role": None, "name": None})

        logger.info(
            "identity_lookup_hit",
            extra={"event": {"tenant_id": tenant_id, "role": principal.role.value}},
        )
        return JSONResponse(
            {
                "found": True,
                "user_id": principal.user_id,
                "role": principal.role.value,
                "name": principal.display_name,
            }
        )

    @app.get("/v1/me")
    def me(authorization: Annotated[str | None, Header()] = None) -> Response:
        """Who this token says you are, and the opening line the widget greets you with.

        The greeting is built here rather than in the widget for the reason every other
        user-facing string is: `app/messages.py` owns the wording, and a per-role greeting written
        in JavaScript would be a second copy of the access rules in `app/security/roles.py` --
        one that drifts silently, promising a customer data they will then be refused.

        Reads nothing but the token. A name is whatever the host application asserted when it
        minted the session; without one the greeting simply has no name in it.
        """
        try:
            claims = verify_session_token(authorization)
        except TokenError as exc:
            return _error(exc.failure, 401)

        return JSONResponse(
            {
                "user_id": claims.principal_id,
                "role": claims.role.value,
                "name": claims.display_name,
                "greeting": chat_welcome(claims.role.value, claims.display_name),
            }
        )

    # Defined with `def`, not `async def`, on purpose: answer_question is blocking (Gemini calls,
    # Mongo round-trips, WeasyPrint) and Starlette runs sync endpoints in a worker thread. As
    # `async def` it would block the event loop for the whole question and serialise every
    # concurrent user behind it.
    @app.post("/v1/ask")
    def ask(payload: AskRequest, authorization: Annotated[str | None, Header()] = None) -> Response:
        try:
            claims = verify_session_token(authorization)
        except TokenError as exc:
            return _error(exc.failure, 401)

        question, rejection = _validated_question(payload.question)
        if rejection is not None:
            return rejection

        result = answer_question(
            question,
            client,
            user_id=claims.principal_id,
            # Derived from the token, never from the body -- see SessionClaims.conversation_id.
            channel_id=claims.conversation_id,
            principal=claims.principal,
        )
        return JSONResponse(
            {"text": result.text, "error": result.error, "file": _file_payload(result, claims)}
        )

    @app.post("/v1/ask/stream")
    def ask_stream(
        payload: AskRequest, authorization: Annotated[str | None, Header()] = None
    ) -> Response:
        """The same answer as /v1/ask, delivered as Server-Sent Events while it is built.

        Four event types: `stage` (understanding / locating / querying / writing / report),
        `token` (a piece of the answer, already redacted), `result` (the complete answer, the
        authoritative text, plus any file), and `error`. A client that only handles `result`
        behaves exactly like a client of /v1/ask.

        `result` carries the full text even though the tokens were already sent, deliberately: it
        is what the answer cache and the audit log recorded, it has been through the finished-text
        PII scan, and progress events may have been dropped under backpressure. The tokens are for
        watching; the result is what is true.
        """
        try:
            claims = verify_session_token(authorization)
        except TokenError as exc:
            # Before the stream opens, so this is an ordinary HTTP error rather than an SSE frame
            # -- a 401 the browser can act on, not a 200 containing bad news.
            return _error(exc.failure, 401)

        question, rejection = _validated_question(payload.question)
        if rejection is not None:
            return rejection

        sink = QueueSink()

        def work() -> None:
            try:
                result = answer_question(
                    question,
                    client,
                    user_id=claims.principal_id,
                    channel_id=claims.conversation_id,
                    principal=claims.principal,
                    progress=sink,
                )
                sink.finish(
                    StreamEvent(
                        kind="result",
                        text=result.text,
                        data={
                            "error": result.error,
                            "file": _file_payload(result, claims),
                        },
                    )
                )
            except Exception:
                # answer_question turns every expected failure into an AnswerResult, so reaching
                # here means something genuinely unhandled. The user still gets the catalogue's
                # wording; the traceback goes to the audit log, as everywhere else.
                logger.exception(
                    "widget_stream_failed", extra={"event": {"user_id": claims.principal_id}}
                )
                reference = new_reference()
                sink.finish(
                    StreamEvent(
                        kind="error",
                        text=failure_message(Failure.UNKNOWN, reference),
                        data={"error": Failure.UNKNOWN.value, "reference": reference},
                    )
                )

        # A thread rather than the endpoint's own: the response has to start streaming *now*, and
        # answer_question blocks for as long as the question takes.
        threading.Thread(target=work, name="widget-answer", daemon=True).start()

        def frames():
            # A comment frame first, so proxies and the browser commit to the response before the
            # first real event -- which may be a second or two away.
            yield ": open\n\n"
            for event in sink:
                yield _sse_frame(event)

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                # nginx buffers proxied responses by default, which holds every event until the
                # answer is finished -- turning a live stream back into a slow page load.
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/v1/files/{file_id}")
    def download(file_id: str, t: Annotated[str | None, Query()] = None) -> Response:
        """Hand over a generated report, once, to the principal it was generated for.

        The token is a query parameter because this URL is followed by the browser itself (an
        `<a href>` / `window.open`), which cannot carry an Authorization header.
        """
        try:
            claims = verify_file_token(t, file_id=file_id)
        except TokenError as exc:
            return _error(exc.failure, 401)

        stored = file_store.get(
            file_id, tenant_id=claims.tenant_id, principal_id=claims.principal_id
        )
        if stored is None:
            # Missing, expired and not-yours are one response on purpose -- see FileStore.get.
            return _error(Failure.SESSION_EXPIRED, 404)

        return Response(
            content=stored.content,
            media_type=_MEDIA_TYPES.get(stored.file_type, "application/octet-stream"),
            headers={
                "Content-Disposition": f'attachment; filename="{stored.filename}"',
                # These bytes are user data rendered from database rows. nosniff stops a browser
                # deciding a CSV is HTML and executing whatever a stored value happens to contain.
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )

    def _playground_gate(request: Request) -> Response | None:
        """The two fences every `/v1/dev/*` endpoint sits behind, or None to proceed.

        Shared rather than repeated: three endpoints now live back here, and a playground endpoint
        that forgot one of these is an open session-minting endpoint on someone's laptop.
        """
        if not settings.widget_dev_playground:
            # 404, not 403: a disabled playground should look absent rather than merely shut.
            return _error(Failure.NOT_AUTHENTICATED, 404)
        client_host = request.client.host if request.client else ""
        if client_host not in _LOOPBACK_HOSTS:
            # A playground reachable from another machine is an open session-minting endpoint.
            # The *peer* address, never a forwarded header -- anyone can claim to be local.
            logger.warning(
                "widget_dev_playground_refused", extra={"event": {"client": client_host}}
            )
            return _error(Failure.NOT_AUTHENTICATED, 403)
        return None

    def _sign_in_response(principal: Principal) -> Response:
        """Set the playground's sign-in cookie and say who was signed in.

        Shared by `/v1/dev/login` and `/v1/dev/signup` so that signing up is signing in -- a
        sign-up that then made you type the same password into a second form would be a worse
        version of the same thing.
        """
        cookie, ttl = mint_dev_login_cookie(tenant_id="default", principal=principal)
        response = JSONResponse(
            {
                "user_id": principal.user_id,
                "role": principal.role.value,
                "name": principal.display_name,
            }
        )
        # HttpOnly so a script on this page can't read it, SameSite=Lax so another site can't ride
        # it. No `Secure`: the playground is loopback-only and http, and a Secure cookie there is
        # a cookie the browser silently discards.
        response.set_cookie(
            _DEV_COOKIE, cookie, max_age=ttl, httponly=True, samesite="lax", path="/"
        )
        logger.info(
            "widget_dev_signed_in",
            extra={"event": {"role": principal.role.value, "user_id": principal.user_id}},
        )
        return response

    def _signed_in_user(request: Request) -> SessionClaims | None:
        """The playground user this request carries, or None. Never raises: not being signed in is
        the ordinary state of the sign-in page."""
        if not settings.widget_dev_playground:
            return None
        try:
            return verify_dev_login_cookie(request.cookies.get(_DEV_COOKIE))
        except TokenError:
            return None

    @app.get("/")
    def landing(request: Request) -> Response:
        """The playground: a sign-in page, and afterwards the chat, scoped to whoever signed in.

        The gateway has no user interface by design, so `/` used to 404 -- correct, and a poor
        first thing to meet. What it shows now depends on who is looking, and that decision is
        made *here* rather than in the page's JavaScript: the endpoint reference and the embedding
        guide are operator documentation, so they are stripped from the HTML entirely for anyone
        who isn't an admin. Hiding them client-side would ship them to everyone and call it a
        preference.

        Everything else is static except a small config object naming whether the playground is
        enabled, which roles may sign in, and who is currently signed in -- all from the server,
        so the page cannot claim a capability the gateway won't honour.
        """
        page = pathlib.Path(f"{_STATIC_DIR}/playground.html").read_text(encoding="utf-8")
        claims = _signed_in_user(request)
        config = {
            "playground": settings.widget_dev_playground,
            "roles": sorted(settings.widget_allowed_session_roles),
            "warning": settings.dev_playground_warning() or "",
            "user": (
                {
                    "user_id": claims.principal_id,
                    "role": claims.role.value,
                    "name": claims.display_name,
                }
                if claims
                else None
            ),
        }
        if claims is None or claims.role is not Role.ADMIN:
            page = _ADMIN_ONLY_BLOCK.sub("", page)
        page = page.replace(
            "<script>\n  (function () {",
            f"<script>\n  window.GATEWAY_CONFIG = {json.dumps(config)};\n  (function () {{",
            1,
        )
        return Response(
            content=page,
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/v1/dev/login")
    def dev_login(payload: LoginRequest, request: Request) -> Response:
        """Sign in to the playground with an email and a password.

        The playground used to ask for a role and a user id, which asked the wrong person the
        wrong question: nobody knows their own `USR-00031`, and a role you type is a role you
        choose. Both now come from the account -- `app/db/identity.py::authenticate_password`
        checks the password against `users.password_hash` and reads the role off `usertype` -- so
        the only thing a person supplies is what they know.

        Still fenced exactly as the rest of the playground is, and rate limited per peer on top:
        this is the one endpoint here that accepts guesses at a stored credential.
        """
        refusal = _playground_gate(request)
        if refusal is not None:
            return refusal

        client_host = request.client.host if request.client else "unknown"
        if not rate_limiter.allow(("dev_login", client_host)):
            return _error(Failure.USER_RATE_LIMITED, 429)

        try:
            principal = authenticate_password(get_db(), payload.email, payload.password)
        except AccountNotActiveError:
            # The password was right. Saying so is safe here and nowhere else -- see
            # app/db/identity.py::AccountNotActiveError -- and folding this into "those don't
            # match" sends someone to re-check a credential that was correct.
            logger.info("widget_dev_login_inactive", extra={"event": {"client": client_host}})
            return _message(
                account_not_active_message(), error="account_not_active", status_code=403
            )
        except Exception:
            # The only endpoint here a *person* meets directly, so a dead database has to read as
            # a sentence rather than as a 500 page: the raw error goes to the audit log with a
            # reference code, exactly as it does everywhere else (app/messages.py).
            reference = new_reference()
            logger.exception("widget_dev_login_failed", extra={"event": {"reference": reference}})
            return _message(
                failure_message(Failure.DATABASE_ERROR, reference),
                error=Failure.DATABASE_ERROR.value,
                status_code=503,
            )

        if principal is None:
            logger.info("widget_dev_login_refused", extra={"event": {"client": client_host}})
            # One message for every reason, including a role this gateway won't mint: telling
            # someone their password was right but their role isn't allowed confirms the account.
            return _message(
                invalid_credentials_message(), error="invalid_credentials", status_code=401
            )

        # The account is real; whether it may hold a session here is the same question
        # /v1/session asks, answered by the same code. The live-account check inside it is
        # redundant with the status check authenticate_password just did, and it stays anyway --
        # sharing this function is what keeps the playground from drifting more permissive.
        role_refusal = _check_session_request(
            "default", principal.user_id or "", principal.role.value
        )
        if role_refusal is not None:
            logger.info(
                "widget_dev_login_role_refused",
                extra={"event": {"role": principal.role.value}},
            )
            return _message(
                invalid_credentials_message(), error="invalid_credentials", status_code=401
            )

        return _sign_in_response(principal)

    @app.post("/v1/dev/signup")
    def dev_signup(payload: SignupRequest, request: Request) -> Response:
        """Create an account, then sign in as it.

        **This is the only request path in the application that writes to MongoDB**, which is why
        it lives behind the playground's two fences and nowhere else: `MONGODB_URI` is documented
        as a read-only credential, and a deployment answering questions has no business creating
        accounts. Against a read-only database this fails cleanly and says so -- see
        `app/db/accounts.py`.

        The account type is bounded by `WIDGET_ALLOWED_SESSION_ROLES`, so nobody can create an
        account in a role the gateway would then refuse to mint a session for -- a form whose
        result is an unusable account is worse than a form that says no. `admin` is not in the
        default, so self-signup as an operator takes a deliberate configuration change.
        """
        refusal = _playground_gate(request)
        if refusal is not None:
            return refusal

        client_host = request.client.host if request.client else "unknown"
        if not rate_limiter.allow(("dev_signup", client_host)):
            return _error(Failure.USER_RATE_LIMITED, 429)

        try:
            role = Role(payload.role.strip().lower())
        except ValueError:
            return _message(unknown_role_message(), error="unknown_role", status_code=400)

        try:
            validate_signup(
                name=payload.name,
                email=payload.email,
                password=payload.password,
                role=role,
                allowed_roles=set(settings.widget_allowed_session_roles),
            )
        except AccountError as exc:
            # A sign-up form's errors name the field, unlike a sign-in's -- see AccountError.
            return _message(str(exc), error="invalid_signup", status_code=400)

        try:
            account = create_account(
                get_db(),
                name=payload.name,
                email=payload.email,
                password=payload.password,
                role=role,
                city=payload.city,
                location=_point(payload.latitude, payload.longitude),
                business_name=payload.business_name,
                category=payload.category,
            )
        except AccountError as exc:
            # The address is taken. 409, not 401: this is the one form where saying so is the
            # whole point, and the person is telling us the address, not guessing it.
            return _message(str(exc), error="account_exists", status_code=409)
        except Exception:
            reference = new_reference()
            logger.exception("widget_dev_signup_failed", extra={"event": {"reference": reference}})
            return _message(
                account_creation_unavailable_message(),
                error="account_creation_unavailable",
                status_code=503,
            )

        principal = Principal(
            role=role, user_id=account["user_id"], display_name=account.get("name")
        )
        return _sign_in_response(principal)

    @app.post("/v1/dev/logout")
    def dev_logout(request: Request) -> Response:
        """Drop the sign-in cookie. Gated like the rest so a disabled playground has no endpoints
        at all, rather than one that quietly does nothing."""
        refusal = _playground_gate(request)
        if refusal is not None:
            return refusal
        response = JSONResponse({"signed_out": True})
        response.delete_cookie(_DEV_COOKIE, path="/")
        return response

    @app.post("/v1/dev/session")
    def dev_session(request: Request) -> Response:
        """Mint a chat session **without a host-app key**, for whoever signed in at `/`.

        This is a deliberate hole in the one credential boundary this service owns, so it is
        fenced four ways: off unless `WIDGET_DEV_PLAYGROUND=true`, refused for any caller that
        isn't on this machine, only for an identity that proved a password, and still subject to
        `WIDGET_ALLOWED_SESSION_ROLES` and the same live-account check as `/v1/session` -- it
        shares `_check_session_request` precisely so a playground session cannot be more
        permissive than a real one.

        The identity comes from the signed cookie rather than a request body because that is what
        the widget's token endpoint contract allows: it POSTs with credentials and no body.
        """
        refusal = _playground_gate(request)
        if refusal is not None:
            return refusal

        try:
            claims = verify_dev_login_cookie(request.cookies.get(_DEV_COOKIE))
        except TokenError as exc:
            return _error(exc.failure, 401)

        # Re-checked on every mint, not just at sign-in: the cookie outlives a chat session on
        # purpose, so suspending an account has to stop it here as well.
        role_refusal = _check_session_request("default", claims.principal_id, claims.role.value)
        if role_refusal is not None:
            return role_refusal

        return _issue_session("default", claims.principal_id, claims.role, claims.display_name)

    @app.get("/widget.js")
    def widget_script() -> Response:
        """The plugin itself, served from the gateway so a host app embeds one URL and no build."""
        return FileResponse(
            f"{_STATIC_DIR}/widget.js",
            media_type="application/javascript; charset=utf-8",
            headers={"Cache-Control": "public, max-age=300"},
        )

    return app
