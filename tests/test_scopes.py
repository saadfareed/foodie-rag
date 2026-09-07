"""Startup scope check: catching a missing `files:write` at boot instead of at first report."""

import logging

from slack_sdk.errors import SlackApiError

from app.slack.scopes import missing_scope_warning


class _FakeResponse:
    def __init__(self, headers):
        self.headers = headers


class _FakeClient:
    """Mimics the slice of WebClient the check uses. `raises` drives the failure paths."""

    def __init__(self, scopes=None, headers=None, raises=None):
        if headers is None:
            headers = {"x-oauth-scopes": scopes} if scopes is not None else {}
        self._headers = headers
        self._raises = raises

    def auth_test(self):
        if self._raises:
            raise self._raises
        return _FakeResponse(self._headers)


def test_no_warning_when_every_required_scope_is_present():
    client = _FakeClient("chat:write,files:write,commands")

    assert missing_scope_warning(client) is None


def test_missing_files_write_is_reported_with_the_fix():
    """This is the real case: an app installed with only incoming-webhook and commands answers
    text fine and silently degrades every report request."""
    client = _FakeClient("incoming-webhook,commands")

    warning = missing_scope_warning(client)

    assert warning is not None
    assert "files:write" in warning
    assert "chat:write" in warning
    # Actionable: names where to change it and that a reinstall is required.
    assert "reinstall" in warning.lower()
    assert "OAuth & Permissions" in warning
    # Reports what *is* granted, so an operator can see the token they actually configured.
    assert "commands" in warning


def test_warning_names_only_the_scopes_actually_missing():
    warning = missing_scope_warning(_FakeClient("chat:write,commands"))

    assert "files:write" in warning
    assert "needed for" in warning


def test_scopes_header_is_matched_case_insensitively():
    """Header casing is transport-dependent and not worth trusting."""
    client = _FakeClient(headers={"X-OAuth-Scopes": "chat:write,files:write"})

    assert missing_scope_warning(client) is None


def test_an_unreadable_scope_header_warns_about_nothing():
    """An absent header means "couldn't determine", not "no scopes granted" -- warning for every
    required scope on a healthy token would train operators to ignore the check."""
    assert missing_scope_warning(_FakeClient(headers={})) is None


def test_an_api_failure_does_not_block_startup(caplog):
    error = SlackApiError("boom", response={"error": "invalid_auth"})
    with caplog.at_level(logging.WARNING, logger="audit"):
        assert missing_scope_warning(_FakeClient(raises=error)) is None

    assert "slack_scope_check_failed" in caplog.text


def test_an_unexpected_exception_does_not_block_startup(caplog):
    """A startup diagnostic must never be the reason the bot won't start."""
    with caplog.at_level(logging.WARNING, logger="audit"):
        assert missing_scope_warning(_FakeClient(raises=ConnectionError("no network"))) is None

    assert "slack_scope_check_failed" in caplog.text
