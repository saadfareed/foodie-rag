"""Startup check that the bot token actually carries the scopes its features need.

A missing scope doesn't fail loudly. Slack rejects the one API call that needed it, at the
moment a user asks for something -- so a bot installed without `files:write` looks fine through
startup, fine through every text answer, and only reveals the problem the first time somebody
asks for a report, as a logged traceback and a degraded reply. That gap between "misconfigured"
and "noticed" is the thing worth closing, which is why this runs at boot alongside
`Settings.pool_size_warning`.

It warns rather than raises, deliberately, and mirrors `pool_size_warning`'s shape. A bot with
no `files:write` is *degraded*, not broken: every text answer still works, and reports still
deliver their prose. Refusing to start would turn a partial outage into a total one over a
feature the operator may not even use.

The granted scopes come from the `x-oauth-scopes` response header, which Slack returns on any
Web API call -- `auth.test` is the cheapest one, and it needs no scopes itself.
"""

import logging

from slack_sdk.errors import SlackApiError
from slack_sdk.web import WebClient

logger = logging.getLogger("audit")

# scope -> what stops working without it. Only scopes this app's own code paths actually call
# are listed; event subscriptions (app_mentions:read, im:history) are configured separately in
# the Slack app manifest and can't be inferred from a token.
REQUIRED_SCOPES: dict[str, str] = {
    "chat:write": "posting answers back to a channel or DM",
    "files:write": "attaching generated CSV/XLSX/PDF reports (they fall back to text)",
}


def _granted_scopes(client: WebClient) -> set[str] | None:
    """Scopes on this token, or None if they couldn't be determined.

    Returning None (rather than an empty set) for an indeterminate result matters: an empty set
    reads as "no scopes granted" and would emit a warning for every required scope on what may
    be a perfectly healthy token behind a transient network blip.
    """
    try:
        response = client.auth_test()
    except SlackApiError as exc:
        logger.warning(
            "slack_scope_check_failed",
            extra={"event": {"error": str(exc.response.get("error", exc))}},
        )
        return None
    except Exception:  # noqa: BLE001 -- a startup diagnostic must never block startup
        logger.warning("slack_scope_check_failed", extra={"event": {"error": "request failed"}})
        return None

    headers = getattr(response, "headers", None) or {}
    # Header lookup is case-insensitive in principle; normalize rather than trusting the casing
    # a given transport happens to preserve.
    raw = next(
        (value for key, value in headers.items() if key.lower() == "x-oauth-scopes"),
        None,
    )
    if raw is None:
        return None
    return {scope.strip() for scope in raw.split(",") if scope.strip()}


def missing_scope_warning(client: WebClient) -> str | None:
    """None if the token has every required scope (or they can't be read), else a warning."""
    granted = _granted_scopes(client)
    if granted is None:
        return None

    missing = {scope: reason for scope, reason in REQUIRED_SCOPES.items() if scope not in granted}
    if not missing:
        return None

    details = "; ".join(f"{scope} (needed for {reason})" for scope, reason in missing.items())
    return (
        f"SLACK_BOT_TOKEN is missing {len(missing)} scope(s): {details}. "
        "Add them under OAuth & Permissions -> Bot Token Scopes at https://api.slack.com/apps, "
        "then reinstall the app to the workspace and update SLACK_BOT_TOKEN -- a scope change "
        "only takes effect on reinstall. "
        f"Granted: {', '.join(sorted(granted)) or '(none)'}."
    )
