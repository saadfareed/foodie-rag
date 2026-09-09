"""The web chat gateway (app/api/).

Organised around the one thing this adapter adds that Slack didn't have to solve: a client that
will send whatever it is told to send. Slack states who is speaking; a browser asserts it. So the
tests that matter most here are the ones proving the *browser cannot influence* identity, scope,
or conversation -- each of which, if it could, fails silently and looks exactly like working.

Everything below the adapter (the graph, Mongo, Gemini) is stubbed: `answer_question` is patched
to record its arguments, which is the whole point -- what reaches it is the assertion.
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from app.api.files import FileStore, StoredFile, file_store
from app.api.server import create_app
from app.api.tokens import SessionClaims, mint_dev_login_cookie, mint_file_token
from app.config import settings
from app.db.accounts import AccountError
from app.db.identity import AccountNotActiveError
from app.messages import Failure, failure_message
from app.rag.pipeline import AnswerResult
from app.security.roles import Principal, Role

SECRET = "test-signing-secret-long-enough-to-pass-the-length-check"
HOST_KEY = "acme:sk_test_hostapp_key"
HOST_SECRET = "sk_test_hostapp_key"

#: Substrings that must never appear in anything this gateway sends a browser. Same idea as
#: tests/test_negative.py::assert_clean -- a list of things that have actually leaked from
#: error paths before, extended rather than re-derived per test.
_INTERNALS = ("traceback", "jwt", "pymongo", "gemini", "mongo", "queryspec", "signature")


@pytest.fixture(autouse=True)
def _widget_settings(monkeypatch):
    """The gateway refuses to start without these (settings.widget_config_error), and they're
    optional at import time so the Slack-only deployment and CI stay unaffected -- so every test
    here supplies them explicitly rather than depending on a .env."""
    monkeypatch.setattr(settings, "widget_jwt_secret", SECRET)
    monkeypatch.setattr(settings, "widget_api_keys", [HOST_KEY])
    monkeypatch.setattr(settings, "widget_session_ttl_seconds", 1800)
    monkeypatch.setattr(settings, "widget_file_ttl_seconds", 600)
    monkeypatch.setattr(settings, "widget_allowed_origins", ["https://shop.example"])
    monkeypatch.setattr(settings, "widget_max_question_chars", 2000)
    monkeypatch.setattr(settings, "widget_allowed_session_roles", ["customer", "vendor"])
    # Pinned rather than inherited, like everything above it. A developer whose .env turns the
    # playground on would otherwise see the "off by default" tests fail on their machine and pass
    # in CI -- the same machine-dependence `conftest.py` pins the rate limits for. The tests that
    # are *about* the playground enable it themselves.
    monkeypatch.setattr(settings, "widget_dev_playground", False)
    monkeypatch.setattr(settings, "widget_verify_asserted_identity", True)


@pytest.fixture
def calls():
    return []


@pytest.fixture
def client(monkeypatch, calls, request):
    """A gateway whose pipeline is a recorder.

    `answer_result` marks a test's desired AnswerResult, so the fake stays a fake rather than
    growing a switch on the question text.
    """
    marker = request.node.get_closest_marker("answer_result")
    result = marker.args[0] if marker else AnswerResult(text="42 orders are pending.")

    def fake_answer_question(question, gemini, **kwargs):
        calls.append({"question": question, **kwargs})
        return result

    monkeypatch.setattr("app.api.server.answer_question", fake_answer_question)
    # Session minting re-checks an asserted vendor/customer against `users` (offboarding -- see
    # settings.widget_verify_asserted_identity). That needs a database, and these tests are about
    # the gateway, so it is stubbed to "yes, still active". The tests below that are *about* the
    # check override it.
    monkeypatch.setattr("app.api.server.principal_exists", lambda db, user_id, role: True)
    monkeypatch.setattr("app.api.server.get_db", lambda: object())
    # An explicit peer address, because TestClient otherwise reports the client as "testclient" --
    # and the dev playground's loopback check is a real guard that has to be exercised as written.
    return TestClient(create_app(gemini=object()), client=("127.0.0.1", 5000))


def _token(client, user_id="user-1", role="vendor", key=HOST_SECRET):
    response = client.post(
        "/v1/session",
        json={"user_id": user_id, "role": role},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


def _ask(client, token, question="how many orders are pending?", **extra):
    return client.post(
        "/v1/ask",
        json={"question": question, **extra},
        headers={"Authorization": f"Bearer {token}"},
    )


# --- the host-app key -------------------------------------------------------------------------


def test_a_session_needs_a_host_app_key(client):
    """Without this, anyone who can reach the gateway can mint a session for any user."""
    response = client.post("/v1/session", json={"user_id": "user-1", "role": "vendor"})

    assert response.status_code == 401
    assert response.json()["error"] == Failure.NOT_AUTHENTICATED.value


def test_a_wrong_host_app_key_is_rejected(client):
    response = client.post(
        "/v1/session",
        json={"user_id": "user-1", "role": "vendor"},
        headers={"Authorization": "Bearer sk_test_not_the_key"},
    )

    assert response.status_code == 401


def test_a_valid_host_app_key_mints_a_usable_session(client):
    response = client.post(
        "/v1/session",
        json={"user_id": "user-1", "role": "vendor"},
        headers={"Authorization": f"Bearer {HOST_SECRET}"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["token"] and body["expires_in"] == 1800
    assert _ask(client, body["token"]).status_code == 200


def test_a_bare_key_entry_falls_back_to_the_default_tenant(client, monkeypatch, calls):
    """WIDGET_API_KEYS entries are `tenant:secret` or a bare secret -- the single-tenant case
    shouldn't have to invent a tenant name to work."""
    monkeypatch.setattr(settings, "widget_api_keys", ["sk_bare_key"])

    token = _token(client, key="sk_bare_key")
    _ask(client, token)

    assert calls[0]["channel_id"] == "web:default:vendor:user-1"


def test_a_secret_containing_a_colon_is_not_split(client, monkeypatch, calls):
    """A generated key very often contains a colon. Splitting on the first one unconditionally
    reinterpreted such a key as `tenant:secret`, so the host application sent the whole string,
    the gateway compared it against the tail, and every sign-in failed with a 401 naming nothing.

    Only a prefix that is a plain identifier means "tenant".
    """
    awkward = "qjga|=B4it!@6F33ny@h$ZK3:P<8XBQ5"
    monkeypatch.setattr(settings, "widget_api_keys", [awkward])

    token = _token(client, key=awkward)
    _ask(client, token)

    assert calls[0]["channel_id"] == "web:default:vendor:user-1"


def test_an_identifier_prefix_still_names_a_tenant(client, monkeypatch, calls):
    monkeypatch.setattr(settings, "widget_api_keys", ["acme-eu:sk_with:colons!inside"])

    token = _token(client, key="sk_with:colons!inside")
    _ask(client, token)

    assert calls[0]["channel_id"] == "web:acme-eu:vendor:user-1"


# --- what the browser cannot do ---------------------------------------------------------------


def test_the_browser_cannot_choose_its_own_data_scope(client, calls):
    """The central guarantee. A scope in the request body must be inert: only the host app's
    server, holding the key, decides which rows a user may see -- exactly what
    app/agents/graph.py's id filters then force onto every generated query.

    If this ever regresses, nothing raises. Every request succeeds and simply answers with
    someone else's data.
    """
    token = _token(client, user_id="USR-00031", role="vendor")

    _ask(client, token, vendor_id="USR-99999", role="admin", scope="admin")

    principal = calls[0]["principal"]
    assert (principal.role, principal.user_id) == (Role.VENDOR, "USR-00031")


def test_a_customer_session_is_scoped_to_that_customer(client, calls):
    token = _token(client, user_id="CUS-7", role="customer")

    _ask(client, token)

    principal = calls[0]["principal"]
    assert (principal.role, principal.user_id) == (Role.CUSTOMER, "CUS-7")


def test_the_conversation_comes_from_the_token_not_the_body(client, calls):
    """A client-chosen conversation id would let one user attach to another's pending
    clarification, follow-up context and per-channel answer cache by guessing a string."""
    token = _token(client, user_id="user-1")

    _ask(client, token, conversation_id="web:acme:someone-else", channel_id="web:acme:admin")

    assert calls[0]["channel_id"] == "web:acme:vendor:user-1"
    assert calls[0]["user_id"] == "user-1"


def test_two_principals_get_separate_conversations(client, calls):
    _ask(client, _token(client, user_id="user-1"))
    _ask(client, _token(client, user_id="user-2"))

    assert calls[0]["channel_id"] != calls[1]["channel_id"]


# --- session tokens ---------------------------------------------------------------------------


def test_asking_without_a_token_is_refused(client, calls):
    response = client.post("/v1/ask", json={"question": "how many orders?"})

    assert response.status_code == 401
    assert response.json()["error"] == Failure.NOT_AUTHENTICATED.value
    assert not calls, "the pipeline ran for an unauthenticated request"


def test_an_expired_token_says_so_rather_than_denying_the_user_exists(client, monkeypatch):
    """ "Reload the page" is actionable; "you aren't signed in" is baffling to someone who is."""
    token = _token(client)
    monkeypatch.setattr(settings, "widget_session_ttl_seconds", -10)
    expired = _token(client)

    assert _ask(client, token).status_code == 200
    response = _ask(client, expired)
    assert response.status_code == 401
    assert response.json()["error"] == Failure.SESSION_EXPIRED.value


def test_a_token_signed_with_another_key_is_rejected(client, monkeypatch, calls):
    token = _token(client)
    monkeypatch.setattr(settings, "widget_jwt_secret", "a-completely-different-secret-key-value")

    assert _ask(client, token).status_code == 401
    assert not calls


def test_a_tampered_token_is_rejected(client, calls):
    token = _token(client)

    assert _ask(client, token[:-4] + "AAAA").status_code == 401
    assert not calls


def test_a_download_token_is_not_a_session_token(client, calls):
    """Both are signed with the same key, so without the `typ` claim a download URL -- which ends
    up in browser history, the address bar and referrers -- would double as proof of identity."""
    file_token = mint_file_token(
        claims=SessionClaims(tenant_id="acme", principal_id="user-1", role=Role.VENDOR),
        file_id="abc",
    )

    assert _ask(client, file_token).status_code == 401
    assert not calls


# --- input bounds -----------------------------------------------------------------------------


def test_an_empty_question_is_answered_not_crashed(client, calls):
    response = _ask(client, _token(client), question="   ")

    assert response.status_code == 400
    assert response.json()["text"]
    assert not calls


def test_an_oversized_question_names_the_limit(client, monkeypatch, calls):
    monkeypatch.setattr(settings, "widget_max_question_chars", 50)

    response = _ask(client, _token(client), question="x" * 51)

    assert response.status_code == 400
    assert "50" in response.json()["text"]
    assert not calls, "an oversized question reached the pipeline"


# --- pipeline outcomes are answers, not transport failures -------------------------------------


@pytest.mark.answer_result(
    AnswerResult(text=failure_message(Failure.USER_RATE_LIMITED), error="user_rate_limited")
)
def test_a_rate_limited_answer_is_delivered_as_an_answer(client):
    """HTTP 200: the bot saying "give it a minute" is the answer, and a 429 here would make the
    widget render a transport error instead of the sentence the user needs."""
    response = _ask(client, _token(client))

    assert response.status_code == 200
    assert response.json()["error"] == "user_rate_limited"
    assert "minute" in response.json()["text"]


# --- generated files --------------------------------------------------------------------------


@pytest.mark.answer_result(
    AnswerResult(text="Here are the pending orders.", file_bytes=b"a,b\n1,2\n", file_type="csv")
)
def test_a_generated_report_round_trips_to_the_browser(client):
    body = _ask(client, _token(client)).json()

    assert body["file"]["type"] == "csv"
    download = client.get(body["file"]["url"])
    assert download.status_code == 200
    assert download.content == b"a,b\n1,2\n"
    assert download.headers["content-disposition"] == 'attachment; filename="report.csv"'
    assert download.headers["x-content-type-options"] == "nosniff"


@pytest.mark.answer_result(
    AnswerResult(text="Here you go.", file_bytes=b"secret,rows\n", file_type="csv")
)
def test_another_users_token_cannot_fetch_the_file(client):
    """The report holds one principal's rows. A download URL that works for anyone holding a
    valid session is a cross-tenant leak wearing a plausible-looking id."""
    body = _ask(client, _token(client, user_id="user-1")).json()
    file_id = body["file"]["id"]

    other = _ask(client, _token(client, user_id="user-2"))
    other_file_token = other.json()["file"]["url"].split("t=")[1]

    response = client.get(f"/v1/files/{file_id}?t={other_file_token}")

    assert response.status_code == 401, "a token minted for another file was accepted"


@pytest.mark.answer_result(AnswerResult(text="Here you go.", file_bytes=b"x", file_type="csv"))
def test_a_download_without_a_token_is_refused(client):
    body = _ask(client, _token(client)).json()

    assert client.get(f"/v1/files/{body['file']['id']}").status_code == 401


def test_a_missing_file_and_someone_elses_file_look_identical(client):
    """Distinguishing them confirms the existence of an id the requester shouldn't be able to
    confirm."""
    store = FileStore(ttl_seconds=600, max_entries=10)
    store.put(
        "abc",
        StoredFile(
            tenant_id="acme",
            principal_id="user-1",
            file_type="csv",
            filename="report.csv",
            content=b"x",
        ),
    )

    assert store.get("abc", tenant_id="acme", principal_id="user-2") is None
    assert store.get("nope", tenant_id="acme", principal_id="user-1") is None
    assert store.get("abc", tenant_id="other", principal_id="user-1") is None
    assert store.get("abc", tenant_id="acme", principal_id="user-1") is not None


def test_the_file_store_is_bounded():
    """Report bytes are megabytes, not the kilobytes the answer cache holds -- unbounded here is
    an out-of-memory bug rather than a slow leak."""
    store = FileStore(ttl_seconds=600, max_entries=2)
    for index in range(4):
        store.put(
            str(index),
            StoredFile(
                tenant_id="acme",
                principal_id="user-1",
                file_type="csv",
                filename="report.csv",
                content=b"x",
            ),
        )

    assert store.get("0", tenant_id="acme", principal_id="user-1") is None
    assert store.get("3", tenant_id="acme", principal_id="user-1") is not None


def test_an_expired_file_is_gone(monkeypatch):
    """A download URL that outlives its answer is an accumulating store of customer rows nobody
    is watching."""
    store = FileStore(ttl_seconds=600, max_entries=10)
    store.put(
        "abc",
        StoredFile(
            tenant_id="acme",
            principal_id="user-1",
            file_type="csv",
            filename="report.csv",
            content=b"x",
        ),
    )
    assert store.get("abc", tenant_id="acme", principal_id="user-1") is not None

    # The TTL clock lives in the state backend (app/state/memory.py), which is where every
    # guardrail's expiry is decided.
    future = time.monotonic() + 601
    monkeypatch.setattr("app.state.memory.time.monotonic", lambda: future)

    assert store.get("abc", tenant_id="acme", principal_id="user-1") is None


def test_a_zero_file_ttl_is_read_as_short_not_forever():
    """The state layer treats a non-positive TTL as "never expires" (a `/login` session uses that
    deliberately). Inheriting it here would turn a typo into a permanent store of query results."""
    store = FileStore(ttl_seconds=0, max_entries=10)

    assert store._store._ttl_seconds == FileStore._MINIMUM_TTL_SECONDS


def test_a_text_only_answer_parks_no_file(client):
    """The inverse of the round-trip test: the store must not fill up on every question. The
    default stubbed answer carries no file, so this is the ordinary case."""
    assert _ask(client, _token(client)).json()["file"] is None
    assert file_store.get("anything", tenant_id="acme", principal_id="user-1") is None


# --- refusing to boot -------------------------------------------------------------------------


def test_the_gateway_refuses_to_start_without_a_signing_key(monkeypatch):
    """Empty-key JWT verification accepts forged tokens, so a gateway with no secret starts fine
    and does no identity checking at all -- which looks exactly like working."""
    monkeypatch.setattr(settings, "widget_jwt_secret", "")

    with pytest.raises(RuntimeError, match="WIDGET_JWT_SECRET"):
        create_app(gemini=object())


def test_the_gateway_refuses_a_short_signing_key(monkeypatch):
    monkeypatch.setattr(settings, "widget_jwt_secret", "too-short")

    with pytest.raises(RuntimeError, match="WIDGET_JWT_SECRET"):
        create_app(gemini=object())


def test_the_gateway_refuses_to_start_without_a_host_app_key(monkeypatch):
    monkeypatch.setattr(settings, "widget_api_keys", [])

    with pytest.raises(RuntimeError, match="WIDGET_API_KEYS"):
        create_app(gemini=object())


# --- everything else --------------------------------------------------------------------------


def test_the_widget_script_is_served_as_javascript(client):
    response = client.get("/widget.js")

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert "textContent" in response.text, "the widget must not have grown an innerHTML path"


def test_health_check(client):
    assert client.get("/healthz").json() == {"status": "ok"}


@pytest.mark.parametrize(
    "failure", [Failure.NOT_AUTHENTICATED, Failure.SESSION_EXPIRED, Failure.UNKNOWN]
)
def test_no_gateway_error_names_an_internal(failure):
    """These strings reach a browser on a customer's site -- the same policy Slack replies are
    held to in tests/test_messages.py."""
    text = failure_message(failure, "E-ABC123").lower()

    for internal in _INTERNALS:
        assert internal not in text, f"{failure} leaks {internal!r}"


# --- streaming ---------------------------------------------------------------------------------


def _events(response) -> list[dict]:
    """Parse an SSE body into {kind, payload} entries."""
    parsed = []
    for block in response.text.split("\n\n"):
        lines = [line for line in block.split("\n") if line and not line.startswith(":")]
        if not lines:
            continue
        kind = next((ln[len("event: ") :] for ln in lines if ln.startswith("event: ")), None)
        data = next((ln[len("data: ") :] for ln in lines if ln.startswith("data: ")), "{}")
        if kind:
            parsed.append({"kind": kind, "payload": json.loads(data)})
    return parsed


def _stream(client, token, question="how many orders are pending?"):
    return client.post(
        "/v1/ask/stream",
        json={"question": question},
        headers={"Authorization": f"Bearer {token}"},
    )


def test_a_stream_ends_with_the_complete_answer(client):
    """A client that handles only `result` behaves exactly like a client of /v1/ask."""
    events = _events(_stream(client, _token(client)))

    assert events[-1]["kind"] == "result"
    assert events[-1]["payload"]["text"] == "42 orders are pending."


def test_a_stream_reports_the_stage_it_is_on(client, monkeypatch):
    """A spinner for eight seconds and a spinner for two look identical, which is what makes the
    wait feel broken."""

    def fake_answer_question(question, gemini, **kwargs):
        kwargs["progress"].stage("understanding")
        kwargs["progress"].stage("querying", "orders")
        return AnswerResult(text="done")

    monkeypatch.setattr("app.api.server.answer_question", fake_answer_question)

    stages = [e for e in _events(_stream(client, _token(client))) if e["kind"] == "stage"]

    assert [s["payload"]["text"] for s in stages] == ["understanding", "querying"]
    assert stages[1]["payload"]["detail"] == "orders"


def test_streamed_tokens_arrive_before_the_result(client, monkeypatch):
    def fake_answer_question(question, gemini, **kwargs):
        for piece in ("42 orders ", "are pending."):
            kwargs["progress"].token(piece)
        return AnswerResult(text="42 orders are pending.")

    monkeypatch.setattr("app.api.server.answer_question", fake_answer_question)

    events = _events(_stream(client, _token(client)))
    kinds = [e["kind"] for e in events]

    assert kinds == ["token", "token", "result"]
    assert "".join(e["payload"]["text"] for e in events[:2]) == events[-1]["payload"]["text"]


def test_a_streamed_report_still_gets_a_download_url(client, monkeypatch):
    def fake_answer_question(question, gemini, **kwargs):
        return AnswerResult(text="Here you go.", file_bytes=b"a,b\n1,2\n", file_type="csv")

    monkeypatch.setattr("app.api.server.answer_question", fake_answer_question)

    result = _events(_stream(client, _token(client)))[-1]

    assert result["payload"]["file"]["type"] == "csv"
    assert client.get(result["payload"]["file"]["url"]).content == b"a,b\n1,2\n"


def test_the_stream_endpoint_is_scoped_exactly_like_the_plain_one(client, calls):
    """Two endpoints calling the same pipeline is two chances to scope one of them differently."""
    token = _token(client, user_id="USR-00031", role="vendor")

    _stream(client, token)

    principal = calls[0]["principal"]
    assert (principal.role, principal.user_id) == (Role.VENDOR, "USR-00031")
    assert calls[0]["channel_id"] == "web:acme:vendor:USR-00031"


def test_streaming_without_a_token_is_an_http_error_not_an_event(client, calls):
    """Before the stream opens, a 401 is something the browser can act on. A 200 carrying bad news
    is not."""
    response = client.post("/v1/ask/stream", json={"question": "how many orders?"})

    assert response.status_code == 401
    assert not calls


def test_an_oversized_question_is_rejected_by_both_endpoints(client, monkeypatch, calls):
    monkeypatch.setattr(settings, "widget_max_question_chars", 50)

    assert _stream(client, _token(client), question="x" * 51).status_code == 400
    assert not calls


def test_an_unhandled_failure_becomes_an_error_event(client, monkeypatch):
    """answer_question turns every expected failure into an AnswerResult, so reaching the error
    path means something genuinely unhandled -- and the user still gets the catalogue's wording."""

    def explode(question, gemini, **kwargs):
        raise RuntimeError("pymongo exploded with a traceback")

    monkeypatch.setattr("app.api.server.answer_question", explode)

    events = _events(_stream(client, _token(client)))

    assert events[-1]["kind"] == "error"
    text = events[-1]["payload"]["text"].lower()
    assert events[-1]["payload"]["reference"] in events[-1]["payload"]["text"]
    for internal in _INTERNALS:
        assert internal not in text


# --- roles ---------------------------------------------------------------------------------------


def test_a_host_key_cannot_mint_an_admin_session_by_default(client, calls):
    """A host-app key is a long-lived secret on someone else's server. The blast radius of leaking
    one should not include minting an unrestricted session, so `admin` is off unless an operator
    turns it on -- this is the one privilege-escalation path the gateway itself owns."""
    response = client.post(
        "/v1/session",
        json={"user_id": "someone", "role": "admin"},
        headers={"Authorization": f"Bearer {HOST_SECRET}"},
    )

    assert response.status_code == 403
    assert response.json()["error"] == "role_not_permitted"


def test_admin_sessions_work_once_explicitly_enabled(client, monkeypatch, calls):
    monkeypatch.setattr(settings, "widget_allowed_session_roles", ["customer", "vendor", "admin"])

    _ask(client, _token(client, user_id="ops-1", role="admin"))

    principal = calls[0]["principal"]
    assert principal.role is Role.ADMIN
    # An admin has no row in `users`, so there is nothing to scope it to -- inventing an id here
    # would suggest otherwise.
    assert principal.user_id is None


def test_an_anonymous_session_cannot_be_minted(client):
    """`anonymous` is a real role in the policy (it can read nothing) but it is not a session
    anyone should be issued -- a token carrying it would be a token that does nothing."""
    response = client.post(
        "/v1/session",
        json={"user_id": "nobody", "role": "anonymous"},
        headers={"Authorization": f"Bearer {HOST_SECRET}"},
    )

    assert response.status_code in (400, 403)


def test_an_unknown_role_is_refused_with_something_actionable(client):
    """A host application's developer reads this, not a user, so it names the valid values."""
    response = client.post(
        "/v1/session",
        json={"user_id": "u", "role": "superuser"},
        headers={"Authorization": f"Bearer {HOST_SECRET}"},
    )

    assert response.status_code == 400
    assert "vendor" in response.json()["text"]


def test_a_role_is_required(client):
    """No default: `admin` would grant everything on a typo, and `customer` would silently
    mis-scope a vendor."""
    response = client.post(
        "/v1/session",
        json={"user_id": "u"},
        headers={"Authorization": f"Bearer {HOST_SECRET}"},
    )

    assert response.status_code == 422


def test_a_token_with_no_role_is_rejected(client, calls):
    """A token minted by an older version of this service must not default to any role -- the two
    available fallbacks are wrong in opposite directions."""
    import jwt

    forged = jwt.encode(
        {"typ": "session", "tid": "acme", "sub": "user-1", "exp": 2**31},
        SECRET,
        algorithm="HS256",
    )

    assert _ask(client, forged).status_code == 401
    assert not calls


# --- identity lookup -----------------------------------------------------------------------------


def _lookup(client, email, key=HOST_SECRET):
    return client.post(
        "/v1/identity/lookup",
        json={"email": email},
        headers={"Authorization": f"Bearer {key}"},
    )


def test_identity_lookup_needs_a_host_app_key(client):
    """It reads a field the field policy denies to everything else. A browser must not reach it."""
    assert client.post("/v1/identity/lookup", json={"email": "a@b.com"}).status_code == 401


def test_identity_lookup_returns_the_role_the_data_encodes(client, monkeypatch):
    monkeypatch.setattr(
        "app.api.server.find_principal_by_email",
        lambda db, email, tenant_id="default": Principal(
            role=Role.VENDOR, user_id="USR-00031", display_name="Kifayat Foods"
        ),
    )
    monkeypatch.setattr("app.api.server.get_db", lambda: object())

    body = _lookup(client, "vendor@example.com").json()

    assert body == {
        "found": True,
        "user_id": "USR-00031",
        "role": "vendor",
        "name": "Kifayat Foods",
    }


def test_identity_lookup_says_nothing_about_why_it_missed(client, monkeypatch):
    """No such address, a suspended account and an unmapped usertype are one response. Any
    difference between them is a way to test whether an address is registered."""
    monkeypatch.setattr(
        "app.api.server.find_principal_by_email", lambda db, email, tenant_id="default": None
    )
    monkeypatch.setattr("app.api.server.get_db", lambda: object())

    body = _lookup(client, "nobody@example.com").json()

    assert body == {"found": False, "user_id": None, "role": None, "name": None}


def test_identity_lookup_is_rate_limited(client, monkeypatch):
    """It is the one endpoint where an address can be tested for existence, so a compromised host
    key must not turn it into an enumeration oracle."""
    monkeypatch.setattr("app.rag.rate_limiter.rate_limiter._limit", 3)
    monkeypatch.setattr(
        "app.api.server.find_principal_by_email", lambda db, email, tenant_id="default": None
    )
    monkeypatch.setattr("app.api.server.get_db", lambda: object())

    codes = [_lookup(client, f"user{i}@example.com").status_code for i in range(5)]

    assert 429 in codes


def test_a_suspended_account_cannot_be_given_a_session(client, monkeypatch):
    """The offboarding hole. Without this, a vendor suspended in the database keeps reading until
    their existing token expires -- and their host application, which still has them logged in,
    happily mints them a new one."""
    monkeypatch.setattr("app.api.server.principal_exists", lambda db, user_id, role: False)

    response = client.post(
        "/v1/session",
        json={"user_id": "USR-00031", "role": "vendor"},
        headers={"Authorization": f"Bearer {HOST_SECRET}"},
    )

    assert response.status_code == 403
    assert response.json()["error"] == "identity_not_active"


def test_an_admin_session_skips_the_account_check(client, monkeypatch):
    """An admin has no row in `users` by design, so a lookup would always fail -- checking it
    would make admin sessions impossible rather than safer."""
    monkeypatch.setattr(settings, "widget_allowed_session_roles", ["admin"])

    def _explode(*args, **kwargs):
        raise AssertionError("an admin session was checked against `users`")

    monkeypatch.setattr("app.api.server.principal_exists", _explode)

    response = client.post(
        "/v1/session",
        json={"user_id": "ops-1", "role": "admin"},
        headers={"Authorization": f"Bearer {HOST_SECRET}"},
    )

    assert response.status_code == 200


def test_the_account_check_can_be_turned_off(client, monkeypatch):
    """For host applications whose users legitimately have no row in `users`."""
    monkeypatch.setattr(settings, "widget_verify_asserted_identity", False)

    def _explode(*args, **kwargs):
        raise AssertionError("the account check ran while disabled")

    monkeypatch.setattr("app.api.server.principal_exists", _explode)

    assert _token(client) is not None


# --- the landing page and its playground --------------------------------------------------------


def _dev_cookie(client, role="vendor", user_id="USR-00031", name="Ayesha Khan"):
    """Sign the playground cookie the way `/v1/dev/login` would, without a database.

    The page's own sign-in is tested separately; these tests are about what happens *after* one,
    and minting the cookie here is what keeps them from depending on a password hash.
    """
    cookie, _ = mint_dev_login_cookie(
        tenant_id="default",
        principal=Principal(role=Role(role), user_id=user_id, display_name=name),
    )
    client.cookies.set("dev_session", cookie)


def test_the_root_page_is_a_sign_in_page(client):
    """`/` used to 404 -- correct for an API, and a poor first thing to meet when you are looking
    for the chat."""
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Sign in" in response.text or "playground" in response.text


def test_the_gateway_reference_is_not_shown_to_anyone_who_is_not_an_admin(client, monkeypatch):
    """Stripped from the HTML server-side, not hidden with CSS: what the endpoints are and how to
    embed the widget is operator documentation, and a page that ships it to everyone and then
    hides it has expressed a preference rather than a rule."""
    monkeypatch.setattr(settings, "widget_dev_playground", True)

    anonymous = client.get("/").text
    _dev_cookie(client, role="customer", user_id="USR-00001")
    customer = client.get("/").text

    for body in (anonymous, customer):
        assert "Data chat gateway" not in body
        assert "/v1/identity/lookup" not in body
        assert "Embedding it" not in body


def test_an_admin_sees_the_gateway_reference_and_the_embedding_guide(client, monkeypatch):
    monkeypatch.setattr(settings, "widget_dev_playground", True)
    monkeypatch.setattr(settings, "widget_allowed_session_roles", ["admin"])
    _dev_cookie(client, role="admin", user_id="USR-00051", name="Operator")

    body = client.get("/").text

    assert "Data chat gateway" in body
    assert "/v1/ask" in body
    assert "examples/supabase-app" in body


def test_the_page_says_who_is_signed_in(client, monkeypatch):
    """The greeting is rendered from this, so it has to come from the server -- a page that took
    the name from its own URL would be greeting whoever asked."""
    monkeypatch.setattr(settings, "widget_dev_playground", True)
    _dev_cookie(client, role="vendor", user_id="USR-00031", name="Ayesha Khan")

    body = client.get("/").text

    assert '"role": "vendor"' in body
    assert "Ayesha Khan" in body


def test_a_signed_out_visitor_is_nobody(client, monkeypatch):
    monkeypatch.setattr(settings, "widget_dev_playground", True)

    assert '"user": null' in client.get("/").text


def test_a_cookie_from_another_signing_key_signs_nobody_in(client, monkeypatch):
    """The cookie names a role. It used to be a plain string the page wrote itself, which was
    honest next to a role dropdown and would be a lie next to a password field."""
    monkeypatch.setattr(settings, "widget_dev_playground", True)
    _dev_cookie(client, role="admin", user_id="USR-1")
    monkeypatch.setattr(settings, "widget_jwt_secret", "a-different-secret-of-adequate-length!!")

    body = client.get("/").text

    assert '"user": null' in body
    assert "Data chat gateway" not in body


def test_the_page_reports_the_playground_as_off_by_default(client):
    body = client.get("/").text

    assert '"playground": false' in body


def test_the_page_only_offers_roles_the_gateway_would_actually_mint(client, monkeypatch):
    """The page must not advertise a capability the gateway refuses -- `admin` is not in the
    default allow-list, and offering it would produce a 403 the user can't explain."""
    monkeypatch.setattr(settings, "widget_allowed_session_roles", ["vendor"])

    body = client.get("/").text

    assert '"roles": ["vendor"]' in body


# --- signing in to the playground ---------------------------------------------------------------


@pytest.fixture
def signs_in(monkeypatch):
    """Turn the playground on and make `authenticate_password` answer with whatever a test wants,
    recording what it was asked. The password check itself is tested in tests/test_identity.py --
    here the subject is the endpoint in front of it."""
    monkeypatch.setattr(settings, "widget_dev_playground", True)
    monkeypatch.setattr(settings, "widget_verify_asserted_identity", False)
    state = {"principal": None, "calls": [], "raises": None}

    def fake_authenticate(db, email, password, **kwargs):
        state["calls"].append({"email": email, "password": password})
        if state["raises"] is not None:
            raise state["raises"]
        return state["principal"]

    monkeypatch.setattr("app.api.server.authenticate_password", fake_authenticate)
    return state


def _login(client, email="usr-00031@example.test", password="test123"):
    return client.post("/v1/dev/login", json={"email": email, "password": password})


def test_signing_in_needs_no_user_id(client, signs_in):
    """The whole point of the change: nobody knows their own `USR-00031`, and a role you type
    into a form is a role you chose rather than one you hold."""
    signs_in["principal"] = Principal(
        role=Role.VENDOR, user_id="USR-00031", display_name="Ayesha Khan"
    )

    response = _login(client)

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "USR-00031",
        "role": "vendor",
        "name": "Ayesha Khan",
    }
    assert signs_in["calls"] == [{"email": "usr-00031@example.test", "password": "test123"}]


def test_the_role_comes_from_the_account_not_the_request(client, signs_in, calls):
    """A browser can post anything. What it posts here reaches nothing that decides scope."""
    signs_in["principal"] = Principal(role=Role.CUSTOMER, user_id="USR-00002")

    client.post(
        "/v1/dev/login",
        json={
            "email": "a@b.test",
            "password": "test123",
            "role": "admin",
            "user_id": "somebody-else",
        },
    )
    token = client.post("/v1/dev/session").json()["token"]
    _ask(client, token)

    assert calls[0]["principal"].role is Role.CUSTOMER
    assert calls[0]["principal"].user_id == "USR-00002"


def test_a_failed_sign_in_says_one_thing_however_it_failed(client, signs_in):
    """Unknown address, wrong password and suspended account are one message. Any difference
    between them is a way to test whether an address is registered."""
    signs_in["principal"] = None

    response = _login(client, email="nobody@example.test")

    assert response.status_code == 401
    assert response.json()["error"] == "invalid_credentials"
    assert "nobody@example.test" not in response.text


def test_a_closed_account_is_told_it_is_closed_not_that_its_password_is_wrong(client, signs_in):
    """The password has already verified by the time this is raised, so naming the reason reveals
    nothing -- and the alternative sends someone to re-check a correct password. A seeded demo
    database with a realistic status spread had 22 of 50 accounts failing exactly this way."""
    signs_in["raises"] = AccountNotActiveError("suspended")

    response = _login(client)

    assert response.status_code == 403
    assert response.json()["error"] == "account_not_active"
    assert "isn't active" in response.json()["text"]


def test_a_closed_account_is_not_told_which_status_it_holds(client, signs_in):
    """ "Suspended" and "inactive" are an operator's vocabulary and an internal value; the person
    signing in needs the door to knock on, not the enum."""
    signs_in["raises"] = AccountNotActiveError("suspended")

    body = _login(client).json()

    assert "suspended" not in body["text"].lower()
    assert "administers" in body["text"]


def test_a_role_the_gateway_will_not_mint_fails_like_a_wrong_password(
    client, signs_in, monkeypatch
):
    """Telling someone their password was right but their role isn't allowed confirms the
    account exists, which is the one thing the sign-in form must not do."""
    monkeypatch.setattr(settings, "widget_allowed_session_roles", ["customer"])
    signs_in["principal"] = Principal(role=Role.VENDOR, user_id="USR-00031")

    response = _login(client)

    assert response.status_code == 401
    assert response.json()["error"] == "invalid_credentials"


def test_signing_in_is_rate_limited(client, signs_in, monkeypatch):
    """The one endpoint here that accepts guesses at a stored credential."""
    monkeypatch.setattr("app.rag.rate_limiter.rate_limiter._limit", 3)
    signs_in["principal"] = None

    codes = [_login(client).status_code for _ in range(5)]

    assert 429 in codes


def test_a_database_that_is_down_reads_as_a_sentence(client, signs_in, monkeypatch):
    """This is the one endpoint here a person meets directly. A raw 500 page is both unreadable
    and the only output channel in this codebase without a policy over it."""

    def _explode(*args, **kwargs):
        raise RuntimeError("connection refused to some-internal-host:27017")

    monkeypatch.setattr("app.api.server.authenticate_password", _explode)

    response = _login(client)

    assert response.status_code == 503
    assert response.json()["error"] == "database_error"
    for internal in _INTERNALS + ("27017", "connection refused"):
        assert internal not in response.text.lower()


def test_signing_in_is_absent_unless_the_playground_is_enabled(client, monkeypatch):
    monkeypatch.setattr(settings, "widget_dev_playground", False)

    assert _login(client).status_code == 404


def test_signing_in_is_refused_from_another_machine(client, signs_in):
    """A playground reachable from another machine is a sign-in form on someone's laptop, exposed
    to whoever can route to it."""
    remote = TestClient(create_app(gemini=object()), client=("203.0.113.9", 5000))

    assert _login(remote).status_code == 403


def test_signing_out_drops_the_cookie(client, monkeypatch):
    monkeypatch.setattr(settings, "widget_dev_playground", True)
    _dev_cookie(client)

    response = client.post("/v1/dev/logout")

    assert response.status_code == 200
    # Asserted on the response rather than on the next request: expiring a cookie is something the
    # browser does with what it is told, and what it is told is the part this endpoint owns.
    assert 'dev_session=""' in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]


# --- signing up -----------------------------------------------------------------------------------


@pytest.fixture
def signs_up(monkeypatch):
    """Turn the playground on and record what `create_account` was asked to create. The write
    itself is tested in tests/test_accounts.py; the subject here is the endpoint in front of it."""
    monkeypatch.setattr(settings, "widget_dev_playground", True)
    state = {"calls": [], "raises": None}

    def fake_create_account(db, **kwargs):
        state["calls"].append(kwargs)
        if state["raises"] is not None:
            raise state["raises"]
        return {"user_id": "USR-00051", "name": kwargs["name"]}

    monkeypatch.setattr("app.api.server.create_account", fake_create_account)
    return state


def _signup(client, **overrides):
    body = {
        "name": "Sana Mirza",
        "email": "sana@example.test",
        "password": "test123",
        "role": "vendor",
        **overrides,
    }
    return client.post("/v1/dev/signup", json=body)


def test_signing_up_creates_an_account_and_signs_in(client, signs_up, calls):
    """A sign-up that then made you type the same password into a second form would be a worse
    version of the same thing."""
    response = _signup(client)

    assert response.status_code == 200
    assert response.json()["role"] == "vendor"
    assert "dev_session" in response.headers.get("set-cookie", "")

    token = client.post("/v1/dev/session").json()["token"]
    _ask(client, token)
    assert calls[0]["principal"].role is Role.VENDOR
    assert calls[0]["principal"].user_id == "USR-00051"


def test_the_account_type_is_bounded_by_the_session_role_allowlist(client, signs_up, monkeypatch):
    """Creating an account in a role the gateway then refuses to mint a session for produces an
    account nobody can use. `admin` is not in the default for exactly this reason."""
    monkeypatch.setattr(settings, "widget_allowed_session_roles", ["customer"])

    response = _signup(client, role="vendor")

    assert response.status_code == 400
    assert signs_up["calls"] == [], "nothing was written"


def test_an_unknown_account_type_is_refused(client, signs_up):
    assert _signup(client, role="wizard").status_code == 400
    assert signs_up["calls"] == []


def test_a_taken_address_says_so(client, signs_up):
    """The one form where naming the reason is the point: the person is telling us the address,
    not guessing it, and "that didn't work" leaves them stuck."""
    signs_up["raises"] = AccountError("An account with that email already exists.")

    response = _signup(client)

    assert response.status_code == 409
    assert response.json()["error"] == "account_exists"


def test_a_database_that_refuses_writes_reads_as_a_sentence(client, signs_up):
    """The expected outcome against a production MONGODB_URI, which is read-only by design -- so
    it has to explain itself rather than produce a 500 page."""
    signs_up["raises"] = RuntimeError("not authorized on 10xengineer to execute command insert")

    response = _signup(client)

    assert response.status_code == 503
    assert response.json()["error"] == "account_creation_unavailable"
    for internal in _INTERNALS + ("not authorized", "insert"):
        assert internal not in response.text.lower()


def test_a_location_is_passed_through_as_a_geojson_point(client, signs_up):
    """GeoJSON is [lng, lat] -- the reverse of how every geolocation API hands them over, and the
    reason this is worth asserting rather than eyeballing."""
    _signup(client, latitude=31.5204, longitude=74.3587)

    assert signs_up["calls"][0]["location"] == {
        "type": "Point",
        "coordinates": [74.3587, 31.5204],
    }


def test_an_impossible_location_is_dropped_not_refused(client, signs_up):
    """A bad fix from a browser must not cost someone their registration."""
    response = _signup(client, latitude=999.0, longitude=74.3587)

    assert response.status_code == 200
    assert signs_up["calls"][0]["location"] is None


def test_signing_up_without_a_location_is_fine(client, signs_up):
    assert _signup(client).status_code == 200
    assert signs_up["calls"][0]["location"] is None


def test_signing_up_is_rate_limited(client, signs_up, monkeypatch):
    monkeypatch.setattr("app.rag.rate_limiter.rate_limiter._limit", 3)

    codes = [_signup(client, email=f"a{i}@example.test").status_code for i in range(5)]

    assert 429 in codes


def test_signing_up_is_absent_unless_the_playground_is_enabled(client, monkeypatch):
    """It is the only write this application makes. It has no business existing in a deployment
    that is only answering questions."""
    monkeypatch.setattr(settings, "widget_dev_playground", False)

    assert _signup(client).status_code == 404


def test_signing_up_is_refused_from_another_machine(client, signs_up):
    remote = TestClient(create_app(gemini=object()), client=("203.0.113.9", 5000))

    assert _signup(remote).status_code == 403


# --- minting a chat session from a playground sign-in --------------------------------------------


def test_the_dev_endpoint_mints_for_a_loopback_caller(client, monkeypatch, calls):
    monkeypatch.setattr(settings, "widget_dev_playground", True)
    monkeypatch.setattr(settings, "widget_verify_asserted_identity", False)
    _dev_cookie(client, role="vendor", user_id="USR-00031")

    response = client.post("/v1/dev/session")

    assert response.status_code == 200
    _ask(client, response.json()["token"])
    assert calls[0]["principal"].role is Role.VENDOR
    assert calls[0]["principal"].user_id == "USR-00031"


def test_the_dev_endpoint_refuses_a_caller_from_another_machine(client, monkeypatch):
    """A playground reachable from another machine is an open session-minting endpoint."""
    monkeypatch.setattr(settings, "widget_dev_playground", True)
    monkeypatch.setattr(settings, "widget_verify_asserted_identity", False)
    _dev_cookie(client)

    # A forwarded header must not be enough: the guard is about the actual peer, and trusting a
    # client-supplied header here would let anyone claim to be local.
    spoofed = client.post("/v1/dev/session", headers={"x-forwarded-for": "203.0.113.9"})
    assert spoofed.status_code == 200, "the real peer is loopback, so this is still allowed"

    remote = TestClient(create_app(gemini=object()), client=("203.0.113.9", 5000))
    _dev_cookie(remote)

    assert remote.post("/v1/dev/session").status_code == 403


def test_the_dev_endpoint_cannot_mint_a_role_the_gateway_refuses(client, monkeypatch):
    """It shares `_check_session_request` with /v1/session precisely so a playground session can
    never be more permissive than a real one."""
    monkeypatch.setattr(settings, "widget_dev_playground", True)
    monkeypatch.setattr(settings, "widget_allowed_session_roles", ["customer"])
    _dev_cookie(client, role="admin", user_id="USR-00051")

    assert client.post("/v1/dev/session").status_code == 403


def test_the_dev_endpoint_still_checks_the_account_is_live(client, monkeypatch):
    """What works in the playground has to be what works in production, or it teaches the wrong
    lesson. Re-checked on every mint, not just at sign-in: the cookie outlives a chat session, so
    suspending an account has to stop it here too."""
    monkeypatch.setattr(settings, "widget_dev_playground", True)
    monkeypatch.setattr(settings, "widget_verify_asserted_identity", True)
    monkeypatch.setattr("app.api.server.principal_exists", lambda db, user_id, role: False)
    _dev_cookie(client, role="vendor", user_id="USR-99999")

    assert client.post("/v1/dev/session").status_code == 403


def test_the_dev_endpoint_needs_an_identity(client, monkeypatch):
    monkeypatch.setattr(settings, "widget_dev_playground", True)

    assert client.post("/v1/dev/session").status_code == 401


def test_the_dev_endpoint_is_absent_unless_enabled(client):
    """Off by default: it mints sessions with no host-app key."""
    response = client.post("/v1/dev/session")

    assert response.status_code == 404


def test_a_session_token_is_not_a_sign_in_cookie(client, monkeypatch):
    """Both are signed with the same key, so both carry a `typ`. Without it, a chat token pasted
    into the cookie jar would be a sign-in."""
    monkeypatch.setattr(settings, "widget_dev_playground", True)
    client.cookies.set("dev_session", _token(client, role="vendor"))

    assert client.post("/v1/dev/session").status_code == 401
    assert '"user": null' in client.get("/").text


# --- who the widget is talking to ---------------------------------------------------------------


def _me(client, token):
    return client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})


def test_the_greeting_names_the_person_and_what_their_role_can_ask(client):
    """Built server-side because `app/messages.py` owns every non-answer string, and because a
    per-role greeting written in the widget would be a second copy of the access rules in
    `app/security/roles.py` -- one that drifts silently and promises a customer data they will
    then be refused."""
    response = client.post(
        "/v1/session",
        json={"user_id": "USR-00031", "role": "vendor", "name": "Ayesha Khan"},
        headers={"Authorization": f"Bearer {HOST_SECRET}"},
    )
    body = _me(client, response.json()["token"]).json()

    assert body["name"] == "Ayesha Khan"
    assert body["role"] == "vendor"
    assert "Ayesha Khan" in body["greeting"]
    assert "ordered from you" in body["greeting"]


def test_each_role_is_told_something_different(client, monkeypatch):
    monkeypatch.setattr(settings, "widget_allowed_session_roles", ["customer", "vendor", "admin"])

    greetings = {
        role: _me(client, _token(client, user_id=f"USR-{role}", role=role)).json()["greeting"]
        for role in ("customer", "vendor", "admin")
    }

    assert len(set(greetings.values())) == 3
    # The customer greeting must not invite the question their own role will refuse.
    assert "customers" not in greetings["customer"]


def test_a_session_with_no_name_is_still_greeted(client):
    """A host application that sends no name gets a greeting without one, not no greeting."""
    body = _me(client, _token(client)).json()

    assert body["name"] is None
    assert body["greeting"].startswith("Hi.")


def test_the_browser_cannot_ask_who_it_is_without_a_token(client):
    assert client.get("/v1/me").status_code == 401


def test_the_name_cannot_be_changed_by_the_browser(client, calls):
    """It rides on the token, so a browser holding one can no more edit its own name than its own
    role -- both would have to be re-minted by the host application's server."""
    token = _token(client)

    assert _me(client, token + "x").status_code == 401
