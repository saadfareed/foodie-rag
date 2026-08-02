import os

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


class Settings:
    def __init__(self) -> None:
        self.slack_bot_token = _require("SLACK_BOT_TOKEN")
        self.slack_app_token = _require("SLACK_APP_TOKEN")
        self.slack_signing_secret = _require("SLACK_SIGNING_SECRET")
        self.gemini_api_key = _require("GEMINI_API_KEY")
        self.mongodb_uri = _require("MONGODB_URI")
        self.mongodb_db_name = _require("MONGODB_DB_NAME")
        allowed = os.environ.get("MONGODB_ALLOWED_COLLECTIONS", "")
        self.mongodb_allowed_collections = [c.strip() for c in allowed.split(",") if c.strip()]

        self.gemini_model = os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview")
        self.mongodb_query_timeout_ms = int(os.environ.get("MONGODB_QUERY_TIMEOUT_MS", "5000"))
        self.mongodb_max_result_limit = int(os.environ.get("MONGODB_MAX_RESULT_LIMIT", "200"))


settings = Settings()
