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


def _int(name: str, default: str) -> int:
    """int(os.environ[name]) with a clear error naming the variable -- a bare int(...) on a
    malformed value (e.g. GEMINI_MAX_RETRIES=three) raises "invalid literal for int() with base
    10: 'three'", which says nothing about which of the ~20 env-backed settings is at fault."""
    raw = os.environ.get(name, default)
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Invalid value for {name}: {raw!r} (must be an integer)") from exc


def _float(name: str, default: str) -> float:
    raw = os.environ.get(name, default)
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"Invalid value for {name}: {raw!r} (must be a number)") from exc


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
        # Tried in order, only on a 429 (rate limit) from gemini_model -- Google's free-tier
        # quota is per-model (GenerateRequestsPerDayPerProjectPerModel-FreeTier), so a model
        # that's out of quota for the day doesn't affect a different model's quota. Empty by
        # default: falling back to an unconfirmed model name would just fail differently, so
        # this is opt-in until the exact model IDs in use are confirmed.
        self.gemini_fallback_models = _parse_list(os.environ.get("GEMINI_FALLBACK_MODELS", ""))
        self.mongodb_query_timeout_ms = _int("MONGODB_QUERY_TIMEOUT_MS", "8000")
        self.mongodb_max_result_limit = _int("MONGODB_MAX_RESULT_LIMIT", "200")

        # Connection-level timeouts so a slow/unreachable Mongo doesn't stall a request for
        # pymongo's 30s default server-selection window. maxPoolSize should stay >=
        # slack_socket_mode_concurrency so DB connections don't become the concurrency ceiling.
        self.mongodb_server_selection_timeout_ms = _int(
            "MONGODB_SERVER_SELECTION_TIMEOUT_MS", "3000"
        )
        self.mongodb_connect_timeout_ms = _int("MONGODB_CONNECT_TIMEOUT_MS", "3000")
        self.mongodb_socket_timeout_ms = _int("MONGODB_SOCKET_TIMEOUT_MS", "8000")
        # Default bumped 20 -> 30: a single question can now fan out to up to
        # AGENT_MAX_FAN_OUT parallel domain queries instead of always issuing exactly one, so
        # the pool needs to cover slack_socket_mode_concurrency (10) * AGENT_MAX_FAN_OUT (3)
        # rather than just slack_socket_mode_concurrency, or requests start queuing on the pool.
        # See pool_size_warning() below, which checks this relationship still holds at startup.
        self.mongodb_max_pool_size = _int("MONGODB_MAX_POOL_SIZE", "30")
        # Clamp on geo_near.max_distance_m, mirroring how MAX_DATE_RANGE_DAYS clamps date spans --
        # keeps a "nearby" question from silently becoming an unbounded/full-scan geo query.
        self.mongodb_max_geo_radius_m = _float("MONGODB_MAX_GEO_RADIUS_M", "50000")

        self.audit_log_level = os.environ.get("AUDIT_LOG_LEVEL", "INFO")
        # Optional second logging sink alongside stdout -- empty (default) means stdout only.
        # Set this to persist audit events (question/user/specs/errors/timings) across container
        # restarts instead of relying entirely on however stdout happens to be captured.
        self.audit_log_file = os.environ.get("AUDIT_LOG_FILE", "")
        self.audit_log_file_max_bytes = _int("AUDIT_LOG_FILE_MAX_BYTES", str(10 * 1024 * 1024))
        self.audit_log_file_backup_count = _int("AUDIT_LOG_FILE_BACKUP_COUNT", "5")

        self.gemini_max_retries = _int("GEMINI_MAX_RETRIES", "3")
        self.gemini_retry_base_delay_seconds = _float("GEMINI_RETRY_BASE_DELAY_SECONDS", "1.0")
        # Hard wall-clock ceiling on retry backoff, independent of gemini_max_retries, so a
        # question can't stall indefinitely on repeated transient errors.
        self.gemini_max_retry_seconds = _float("GEMINI_MAX_RETRY_SECONDS", "20.0")
        # Client-side HTTP timeout for Gemini API calls (ms) -- bounds a hung request that would
        # otherwise never fail on its own. A slow-but-transient response past this point is now
        # retried (see _is_retryable), not just failed outright.
        self.gemini_request_timeout_ms = _int("GEMINI_REQUEST_TIMEOUT_MS", "15000")
        # Row cap for the answer-generation prompt -- keeps prompt size (and latency) bounded
        # regardless of mongodb_max_result_limit.
        self.gemini_answer_max_rows = _int("GEMINI_ANSWER_MAX_ROWS", "30")
        # Output-token cap for the query-generation call specifically -- it only needs to emit a
        # small JSON object. Generous by default: on "thinking" models, reasoning tokens count
        # against this budget too, so a too-small cap truncates the JSON mid-object rather than
        # actually saving latency (this happened in production at 512 -- see
        # gemini_query_thinking_budget below, which is the real latency fix).
        self.gemini_query_max_output_tokens = _int("GEMINI_QUERY_MAX_OUTPUT_TOKENS", "2048")
        # Query generation is deterministic structured extraction, not open-ended reasoning --
        # disabling "thinking" (0 = disabled) removes the invisible reasoning-token latency/budget
        # cost entirely for this call. -1 would mean "automatic" (model decides); 0 is explicit off.
        self.gemini_query_thinking_budget = _int("GEMINI_QUERY_THINKING_BUDGET", "0")
        # 0 means unlimited
        self.gemini_daily_call_budget = _int("GEMINI_DAILY_CALL_BUDGET", "0")
        # Circuit breaker (app/llm/circuit_breaker.py): after this many consecutive Gemini
        # failures, stop even trying for gemini_circuit_breaker_cooldown_seconds -- fails fast
        # with a clean message instead of every Socket Mode worker thread independently paying
        # the full retry/timeout cost against a Gemini/network outage that isn't going to
        # resolve within one request's lifetime. 0 disables the breaker entirely.
        self.gemini_circuit_breaker_threshold = _int("GEMINI_CIRCUIT_BREAKER_THRESHOLD", "5")
        self.gemini_circuit_breaker_cooldown_seconds = _float(
            "GEMINI_CIRCUIT_BREAKER_COOLDOWN_SECONDS", "30.0"
        )

        # In-process per-channel answer cache (app/rag/answer_cache.py) -- avoids repeating
        # identical Gemini + Mongo round-trips for a repeated question within the same channel.
        self.answer_cache_ttl_seconds = _int("ANSWER_CACHE_TTL_SECONDS", "1800")
        self.answer_cache_max_entries = _int("ANSWER_CACHE_MAX_ENTRIES", "500")

        self.slack_allowed_channel_ids = _parse_list(
            os.environ.get("SLACK_ALLOWED_CHANNEL_IDS", "")
        )
        self.slack_allowed_user_ids = _parse_list(os.environ.get("SLACK_ALLOWED_USER_IDS", ""))

        # Thread pool size for the Socket Mode client (slack_sdk default is 10); explicit here so
        # it can be tuned alongside mongodb_max_pool_size instead of relying on a hidden default.
        self.slack_socket_mode_concurrency = _int("SLACK_SOCKET_MODE_CONCURRENCY", "10")

        # Multi-domain agent routing (app/agents/) -- classification confidence below this
        # threshold, or a question needing geo with no location given, triggers a clarifying
        # question instead of guessing at a domain.
        self.agent_classifier_min_confidence = _float("AGENT_CLASSIFIER_MIN_CONFIDENCE", "0.55")
        # Caps how many domain agents a single question can fan out to. Only 3 domains
        # (orders/customers/vendors) exist today so this can't yet be hit, but it bounds
        # future growth of the domain registry instead of relying on "there are only 3".
        self.agent_max_fan_out = _int("AGENT_MAX_FAN_OUT", "3")
        # After this many unresolved clarification round-trips for the same (channel, user),
        # give up with an honest message instead of asking again indefinitely.
        self.agent_max_clarification_rounds = _int("AGENT_MAX_CLARIFICATION_ROUNDS", "2")
        # TTL for app/rag/clarification_cache.py entries -- how long a "pending clarification"
        # stays valid before a later, unrelated message from the same user is treated as a new
        # question instead of a follow-up answer.
        self.clarification_cache_ttl_seconds = _int("CLARIFICATION_CACHE_TTL_SECONDS", "300")
        # TTL for app/rag/conversation_context.py entries -- how long the *last resolved
        # question* for a (channel, user) stays available for the classifier to fold into a
        # short follow-up (e.g. "total amount?" after "how many orders?"). Deliberately short
        # and single-turn: this is conversational continuity within one active exchange, not a
        # long-term memory feature -- see app/agents/classifier.py's context_mode field, which
        # decides per-message whether this cache is even consulted.
        self.conversation_context_ttl_seconds = _int("CONVERSATION_CONTEXT_TTL_SECONDS", "300")
        # TTL for app/rag/context_switch_cache.py entries -- how long an unanswered "should I
        # clear the context and treat this as a new question?" prompt stays valid before a later
        # message is treated as an ordinary fresh question instead of a reply to it. Short and
        # separate from conversation_context_ttl_seconds: this is "waiting on a yes/no right now,"
        # not "how long is old context worth remembering."
        self.context_switch_confirmation_ttl_seconds = _int(
            "CONTEXT_SWITCH_CONFIRMATION_TTL_SECONDS", "120"
        )

        # Per-(channel,user) sliding-window rate limit (app/rag/rate_limiter.py) -- independent of
        # gemini_daily_call_budget (a *shared* ceiling): this bounds one user's request rate so a
        # single chatty user can't burn through that shared daily budget alone. 0 disables it.
        self.user_rate_limit_per_minute = _int("USER_RATE_LIMIT_PER_MINUTE", "0")
        self.user_rate_limit_window_seconds = _float("USER_RATE_LIMIT_WINDOW_SECONDS", "60.0")

    def pool_size_warning(self) -> str | None:
        """None if mongodb_max_pool_size comfortably covers worst-case concurrent DB usage,
        otherwise a human-readable warning. Worst case is every Socket Mode worker thread
        simultaneously running a question that fans out to agent_max_fan_out parallel domain
        queries -- if the pool is smaller than that, requests start queuing on pool checkout,
        silently reintroducing the latency these timeouts were meant to bound. Checked explicitly
        at startup (see app/main.py) rather than asserted in __init__, since a too-small pool is a
        performance warning, not something that should crash the process."""
        required = self.slack_socket_mode_concurrency * self.agent_max_fan_out
        if self.mongodb_max_pool_size >= required:
            return None
        return (
            f"MONGODB_MAX_POOL_SIZE ({self.mongodb_max_pool_size}) is below "
            f"SLACK_SOCKET_MODE_CONCURRENCY * AGENT_MAX_FAN_OUT ({required}) -- DB connections "
            "may become the concurrency bottleneck under load. Raise MONGODB_MAX_POOL_SIZE to "
            f"at least {required}."
        )


settings = Settings()
