import os

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _parse_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings:
    def __init__(self) -> None:
        self.slack_bot_token = _require("SLACK_BOT_TOKEN")
        self.slack_app_token = _require("SLACK_APP_TOKEN")
        self.slack_signing_secret = _require("SLACK_SIGNING_SECRET")
        self.gemini_api_key = _require("GEMINI_API_KEY")
        self.mongodb_uri = _require("MONGODB_URI")
        self.mongodb_db_name = _require("MONGODB_DB_NAME")
        self.mongodb_allowed_collections = _parse_list(
            os.environ.get("MONGODB_ALLOWED_COLLECTIONS", "")
        )

        self.gemini_model = os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview")
        self.mongodb_query_timeout_ms = int(os.environ.get("MONGODB_QUERY_TIMEOUT_MS", "5000"))
        self.mongodb_max_result_limit = int(os.environ.get("MONGODB_MAX_RESULT_LIMIT", "200"))

        self.audit_log_level = os.environ.get("AUDIT_LOG_LEVEL", "INFO")

        self.gemini_max_retries = int(os.environ.get("GEMINI_MAX_RETRIES", "3"))
        self.gemini_retry_base_delay_seconds = float(
            os.environ.get("GEMINI_RETRY_BASE_DELAY_SECONDS", "1.0")
        )
        # 0 means unlimited
        self.gemini_daily_call_budget = int(os.environ.get("GEMINI_DAILY_CALL_BUDGET", "0"))

        self.slack_allowed_channel_ids = _parse_list(
            os.environ.get("SLACK_ALLOWED_CHANNEL_IDS", "")
        )
        self.slack_allowed_user_ids = _parse_list(os.environ.get("SLACK_ALLOWED_USER_IDS", ""))


settings = Settings()
