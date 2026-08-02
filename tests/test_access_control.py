from app.slack.access_control import is_authorized


def test_open_when_no_allow_lists_configured(monkeypatch):
    monkeypatch.setattr("app.slack.access_control.settings.slack_allowed_channel_ids", [])
    monkeypatch.setattr("app.slack.access_control.settings.slack_allowed_user_ids", [])
    assert is_authorized("C123", "U123") is True
    assert is_authorized(None, None) is True


def test_channel_allow_list_enforced(monkeypatch):
    monkeypatch.setattr("app.slack.access_control.settings.slack_allowed_channel_ids", ["C1"])
    monkeypatch.setattr("app.slack.access_control.settings.slack_allowed_user_ids", [])
    assert is_authorized("C1", "U123") is True
    assert is_authorized("C2", "U123") is False


def test_user_allow_list_enforced(monkeypatch):
    monkeypatch.setattr("app.slack.access_control.settings.slack_allowed_channel_ids", [])
    monkeypatch.setattr("app.slack.access_control.settings.slack_allowed_user_ids", ["U1"])
    assert is_authorized("C123", "U1") is True
    assert is_authorized("C123", "U2") is False


def test_both_lists_must_pass(monkeypatch):
    monkeypatch.setattr("app.slack.access_control.settings.slack_allowed_channel_ids", ["C1"])
    monkeypatch.setattr("app.slack.access_control.settings.slack_allowed_user_ids", ["U1"])
    assert is_authorized("C1", "U1") is True
    assert is_authorized("C1", "U2") is False
    assert is_authorized("C2", "U1") is False
