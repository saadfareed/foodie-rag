"""Restrict which Slack channels/users may query the bot.

Empty allow-lists mean "open" -- same convention as MONGODB_ALLOWED_COLLECTIONS in
app/config.py: an unset list doesn't restrict anything.
"""

from app.config import settings


def is_authorized(channel_id: str | None, user_id: str | None) -> bool:
    if settings.slack_allowed_channel_ids and channel_id not in settings.slack_allowed_channel_ids:
        return False
    if settings.slack_allowed_user_ids and user_id not in settings.slack_allowed_user_ids:
        return False
    return True
