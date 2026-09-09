"""`/login` vendor sessions: scoping, expiry, and logout.

These matter more than a mock-auth module usually would, because the session decides *which rows
a question returns* (app/security/roles.py forces vendor_id onto the query). A
session that outlives its welcome doesn't just linger -- it silently changes answers.
"""

from app.slack import auth
from app.slack.auth import clear_all, get_authenticated_vendor, login_vendor, logout_vendor
from app.state.store import TtlStore


def _store_with_ttl(ttl_seconds: float) -> TtlStore:
    """A session store with a chosen TTL, for the expiry tests.

    `auth._store` is built once at import from VENDOR_SESSION_TTL_SECONDS, so patching the
    setting afterwards changes nothing -- the TTL is already baked into the store.
    """
    return TtlStore(namespace="vendor_sessions", ttl_seconds=ttl_seconds, max_entries=1000)


def test_login_then_read_returns_the_vendor():
    login_vendor("U1", "USR-00031")

    assert get_authenticated_vendor("U1") == "USR-00031"


def test_sessions_are_per_slack_user():
    login_vendor("U1", "USR-1")
    login_vendor("U2", "USR-2")

    assert get_authenticated_vendor("U1") == "USR-1"
    assert get_authenticated_vendor("U2") == "USR-2"


def test_logging_in_again_replaces_the_previous_session():
    login_vendor("U1", "USR-1")
    login_vendor("U1", "USR-2")

    assert get_authenticated_vendor("U1") == "USR-2"


def test_an_unknown_user_has_no_vendor():
    assert get_authenticated_vendor("nobody") is None


def test_a_missing_user_id_is_not_authenticated():
    """Slack can hand us an event with no user (e.g. some bot/system messages); that must read
    as "not signed in", not blow up."""
    assert get_authenticated_vendor(None) is None
    assert logout_vendor(None) is False


def test_logout_ends_the_session():
    login_vendor("U1", "USR-1")

    assert logout_vendor("U1") is True
    assert get_authenticated_vendor("U1") is None


def test_logout_reports_when_there_was_nothing_to_end():
    """The handler uses this to say "you weren't signed in" rather than a misleading success."""
    assert logout_vendor("U1") is False


def test_a_session_expires_after_its_ttl(monkeypatch):
    """An abandoned session in a long-lived process would otherwise keep a scoped identity
    attached to a Slack user indefinitely, quietly changing which rows their questions return."""
    # The TTL is fixed when the store is built at import, so a test that changes it has to
    # rebuild the store rather than patch the setting.
    monkeypatch.setattr(auth, "_store", _store_with_ttl(60))
    clock = [1000.0]
    monkeypatch.setattr("app.state.memory.time.monotonic", lambda: clock[0])

    login_vendor("U1", "USR-1")
    clock[0] += 59
    assert get_authenticated_vendor("U1") == "USR-1"

    clock[0] += 2  # now 61s old, past the TTL
    assert get_authenticated_vendor("U1") is None


def test_an_expired_session_is_evicted_on_read(monkeypatch):
    """Expiry is observed only here, so evicting on read is what keeps the dict from growing
    without a background sweep."""
    # The TTL is fixed when the store is built at import, so a test that changes it has to
    # rebuild the store rather than patch the setting.
    monkeypatch.setattr(auth, "_store", _store_with_ttl(60))
    clock = [1000.0]
    monkeypatch.setattr("app.state.memory.time.monotonic", lambda: clock[0])

    login_vendor("U1", "USR-1")
    clock[0] += 61
    get_authenticated_vendor("U1")

    assert auth._store.get_json("U1") is None


def test_a_zero_ttl_disables_expiry(monkeypatch):
    monkeypatch.setattr(auth, "_store", _store_with_ttl(0))
    clock = [1000.0]
    monkeypatch.setattr("app.state.memory.time.monotonic", lambda: clock[0])

    login_vendor("U1", "USR-1")
    clock[0] += 10_000

    assert get_authenticated_vendor("U1") == "USR-1"


def test_clear_all_drops_every_session():
    login_vendor("U1", "USR-1")
    login_vendor("U2", "USR-2")

    clear_all()

    assert get_authenticated_vendor("U1") is None
    assert get_authenticated_vendor("U2") is None
