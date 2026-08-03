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
        self.mongodb_query_timeout_ms = int(os.environ.get("MONGODB_QUERY_TIMEOUT_MS", "8000"))
        self.mongodb_max_result_limit = int(os.environ.get("MONGODB_MAX_RESULT_LIMIT", "200"))

        # Connection-level timeouts so a slow/unreachable Mongo doesn't stall a request for
        # pymongo's 30s default server-selection window. maxPoolSize should stay >=
        # slack_socket_mode_concurrency so DB connections don't become the concurrency ceiling.
        self.mongodb_server_selection_timeout_ms = int(
            os.environ.get("MONGODB_SERVER_SELECTION_TIMEOUT_MS", "3000")
        )
        self.mongodb_connect_timeout_ms = int(os.environ.get("MONGODB_CONNECT_TIMEOUT_MS", "3000"))
        self.mongodb_socket_timeout_ms = int(os.environ.get("MONGODB_SOCKET_TIMEOUT_MS", "8000"))
        self.mongodb_max_pool_size = int(os.environ.get("MONGODB_MAX_POOL_SIZE", "20"))

        self.audit_log_level = os.environ.get("AUDIT_LOG_LEVEL", "INFO")

        self.gemini_max_retries = int(os.environ.get("GEMINI_MAX_RETRIES", "3"))
        self.gemini_retry_base_delay_seconds = float(
            os.environ.get("GEMINI_RETRY_BASE_DELAY_SECONDS", "1.0")
        )
        # Hard wall-clock ceiling on retry backoff, independent of gemini_max_retries, so a
        # question can't stall indefinitely on repeated transient errors.
        self.gemini_max_retry_seconds = float(os.environ.get("GEMINI_MAX_RETRY_SECONDS", "20.0"))
        # Client-side HTTP timeout for Gemini API calls (ms) -- bounds a hung request that would
        # otherwise never fail on its own. A slow-but-transient response past this point is now
        # retried (see _is_retryable), not just failed outright.
        self.gemini_request_timeout_ms = int(os.environ.get("GEMINI_REQUEST_TIMEOUT_MS", "15000"))
        # Row cap for the answer-generation prompt -- keeps prompt size (and latency) bounded
        # regardless of mongodb_max_result_limit.
        self.gemini_answer_max_rows = int(os.environ.get("GEMINI_ANSWER_MAX_ROWS", "30"))
        # Output-token cap for the query-generation call specifically -- it only needs to emit a
        # small JSON object. Generous by default: on "thinking" models, reasoning tokens count
        # against this budget too, so a too-small cap truncates the JSON mid-object rather than
        # actually saving latency (this happened in production at 512 -- see
        # gemini_query_thinking_budget below, which is the real latency fix).
        self.gemini_query_max_output_tokens = int(
            os.environ.get("GEMINI_QUERY_MAX_OUTPUT_TOKENS", "2048")
        )
        # Query generation is deterministic structured extraction, not open-ended reasoning --
        # disabling "thinking" (0 = disabled) removes the invisible reasoning-token latency/budget
        # cost entirely for this call. -1 would mean "automatic" (model decides); 0 is explicit off.
        self.gemini_query_thinking_budget = int(os.environ.get("GEMINI_QUERY_THINKING_BUDGET", "0"))
        # 0 means unlimited
        self.gemini_daily_call_budget = int(os.environ.get("GEMINI_DAILY_CALL_BUDGET", "0"))

        # In-process per-channel answer cache (app/rag/answer_cache.py) -- avoids repeating
        # identical Gemini + Mongo round-trips for a repeated question within the same channel.
        self.answer_cache_ttl_seconds = int(os.environ.get("ANSWER_CACHE_TTL_SECONDS", "1800"))
        self.answer_cache_max_entries = int(os.environ.get("ANSWER_CACHE_MAX_ENTRIES", "500"))

        self.slack_allowed_channel_ids = _parse_list(
            os.environ.get("SLACK_ALLOWED_CHANNEL_IDS", "")
        )
        self.slack_allowed_user_ids = _parse_list(os.environ.get("SLACK_ALLOWED_USER_IDS", ""))

        # Thread pool size for the Socket Mode client (slack_sdk default is 10); explicit here so
        # it can be tuned alongside mongodb_max_pool_size instead of relying on a hidden default.
        self.slack_socket_mode_concurrency = int(
            os.environ.get("SLACK_SOCKET_MODE_CONCURRENCY", "10")
        )


settings = Settings()
