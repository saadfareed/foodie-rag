import os

from dotenv import load_dotenv

load_dotenv()


#: The shortest deadline the Gemini API will accept. Below it every call fails immediately with
#: `400 INVALID_ARGUMENT`, so this is a property of the upstream, not a tuning choice of ours.
GEMINI_MIN_REQUEST_TIMEOUT_MS = 10_000


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _parse_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_role_map(name: str, raw: str) -> dict[str, int]:
    """`"admin:60,vendor:20"` -> `{"admin": 60, "vendor": 20}`.

    Empty means "no per-role override" rather than "no limit" -- a caller falls back to the flat
    setting. A malformed entry raises naming the variable, for the same reason `_int` does: a
    limit that silently failed to parse is a limit that silently isn't enforced.
    """
    parsed: dict[str, int] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        role, separator, value = entry.partition(":")
        if not separator:
            raise RuntimeError(f"Invalid value for {name}: {entry!r} (expected 'role:number')")
        try:
            parsed[role.strip().lower()] = int(value)
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid value for {name}: {entry!r} (expected 'role:number')"
            ) from exc
    return parsed


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


# Marker used by every secret placeholder in .env.example, so a sample can show the *shape* of a
# credential without that value ever being accepted as a real one. Matched case-insensitively and
# as a substring, so it survives being embedded in a longer, more descriptive placeholder.
_PLACEHOLDER_MARKER = "change_me"


def _is_placeholder(value: str) -> bool:
    """True if this value is a .env.example placeholder rather than a real credential."""
    return _PLACEHOLDER_MARKER in value.lower()


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
        # Default bumped 30 -> 40: a single question fans out to up to AGENT_MAX_FAN_OUT
        # parallel domain queries *and* may issue anchor-resolution queries ahead of them, so
        # the pool needs to cover slack_socket_mode_concurrency (10) * (AGENT_MAX_FAN_OUT + 1)
        # rather than just concurrency * fan_out, or requests start queuing on the pool.
        # See pool_size_warning() below, which checks this relationship still holds at startup.
        self.mongodb_max_pool_size = _int("MONGODB_MAX_POOL_SIZE", "40")
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
        # Hard wall-clock ceiling on retrying one Gemini call, measured from the start of the
        # first attempt, so a question can't stall indefinitely on repeated transient errors.
        #
        # **This must be larger than gemini_request_timeout_ms, or slow failures are never
        # retried at all.** The ceiling covers the attempts as well as the backoff between them,
        # so a call that fails *slowly* has already spent the budget by the time the first retry
        # is considered. That is not hypothetical: with a 15s timeout and an 8s ceiling, a
        # production `504 DEADLINE_EXCEEDED` arriving 12.1s into query generation was refused a
        # retry it was fully entitled to -- and so was every client-side timeout, which is the
        # exact case _is_retryable was extended to cover. The two guards each read correctly on
        # their own and cancelled each other out. `retry_budget_warning()` now says so at startup.
        #
        # 16s is two 8s attempts plus backoff. It was briefly 8.0 (lowered from 20.0) to bound
        # per-question latency, which is a real concern -- one question makes several calls -- but
        # the right lever for that is the per-attempt timeout below, which now does the bounding.
        self.gemini_max_retry_seconds = _float("GEMINI_MAX_RETRY_SECONDS", "16.0")
        # Client-side HTTP timeout for Gemini API calls (ms) -- bounds a hung request that would
        # otherwise never fail on its own, and it is what actually caps per-question latency.
        #
        # **The API imposes its own floor and rejects anything under it**, with
        # `400 INVALID_ARGUMENT: Manually set deadline 8s is too short. Minimum allowed deadline
        # is 10s.` -- on every call, instantly, so the whole application stops working. An 8s
        # default shipped and did exactly that. The value is therefore clamped rather than
        # trusted: a number the upstream rejects outright is not a preference an operator can
        # hold, and the same reasoning app/api/files.py uses for a non-positive TTL applies here.
        # gemini_timeout_warning() says so at startup when it happens.
        #
        # 10s, not the previous 15s: the point of lowering it is to leave room inside
        # gemini_max_retry_seconds for a second attempt, and the floor is as low as that can go.
        requested_timeout_ms = _int("GEMINI_REQUEST_TIMEOUT_MS", str(GEMINI_MIN_REQUEST_TIMEOUT_MS))
        self.gemini_request_timeout_ms = max(requested_timeout_ms, GEMINI_MIN_REQUEST_TIMEOUT_MS)
        #: What was actually configured, kept only so the warning can name it.
        self._requested_gemini_timeout_ms = requested_timeout_ms
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
        # Default is deliberately ON (was 0/disabled): a single question costs several Gemini
        # calls against a free-tier daily quota, so an unbounded user can exhaust the whole
        # workspace's budget -- and the Socket Mode thread pool -- on their own in under a minute.
        self.user_rate_limit_per_minute = _int("USER_RATE_LIMIT_PER_MINUTE", "10")
        self.user_rate_limit_window_seconds = _float("USER_RATE_LIMIT_WINDOW_SECONDS", "60.0")

        # --- Report generation (app/generators/) ---------------------------------------------
        # Row cap for a generated CSV/XLSX/PDF export. Bounds both the render cost (WeasyPrint
        # and openpyxl are the most expensive CPU on the request path) and the size of the file
        # pushed to Slack. Separate from mongodb_max_result_limit, which bounds what's fetched.
        self.report_max_rows = _int("REPORT_MAX_ROWS", "1000")
        # Column cap per table. A wide Mongo document renders an unreadable table and a huge PDF;
        # this keeps the deterministic column ordering in app/generators/tabular.py bounded.
        self.report_max_columns = _int("REPORT_MAX_COLUMNS", "12")
        # Max distinct categories plotted before a pie chart is rejected in favour of a bar
        # chart -- a pie with 30 slices communicates nothing.
        self.report_max_pie_slices = _int("REPORT_MAX_PIE_SLICES", "8")
        # Bounded pool for PDF/XLSX rendering, so heavy CPU work is capped independently of
        # slack_socket_mode_concurrency. Without this, every Socket Mode worker can be inside
        # WeasyPrint at once and the box thrashes. See app/generators/render_pool.py.
        self.report_render_concurrency = _int("REPORT_RENDER_CONCURRENCY", "2")
        # Wall-clock ceiling on a single render, after which the user gets the text answer
        # instead of waiting indefinitely on a pathological table.
        self.report_render_timeout_seconds = _float("REPORT_RENDER_TIMEOUT_SECONDS", "25.0")
        self.report_title = os.environ.get("REPORT_TITLE", "Data Report")

        # --- Security (app/security/) -------------------------------------------------------
        # Extra field names (comma-separated) to drop from every row before it reaches the LLM,
        # a generated file, the cache, or the audit log -- on top of the built-in internal/secret
        # patterns in app/security/field_policy.py. Names are matched case-insensitively.
        self.security_extra_denied_fields = _parse_list(
            os.environ.get("SECURITY_EXTRA_DENIED_FIELDS", "")
        )
        # How long a `/login` vendor session stays valid (app/slack/auth.py). Bounded so an
        # abandoned session can't keep a scoped identity alive indefinitely in a long-lived
        # process.
        self.vendor_session_ttl_seconds = _int("VENDOR_SESSION_TTL_SECONDS", "3600")

        # --- Roles and row-level access (app/security/roles.py) ------------------------------
        # Cap on the number of ids a computed authorization scope may contain -- today that is
        # "the customers who have ordered from this vendor", read out of `orders` before the
        # fan-out. Past the cap the domain is refused with a "narrow your question" message rather
        # than answered from a truncated set: a truncated set produces an answer that looks
        # complete and is quietly about an arbitrary subset.
        self.rbac_max_authorized_ids = _int("RBAC_MAX_AUTHORIZED_IDS", "1000")
        # The role a Slack user has before `/login`. Slack access is already gated by
        # SLACK_ALLOWED_CHANNEL_IDS / SLACK_ALLOWED_USER_IDS, and historically an un-logged-in
        # Slack user saw everything -- so the default preserves that. Set it to `anonymous` if the
        # people in those channels are not all trusted as operators; the web adapter has no such
        # default and always requires an explicit role in the session token.
        self.slack_default_role = os.environ.get("SLACK_DEFAULT_ROLE", "admin").strip().lower()

        # --- Per-role usage limits ------------------------------------------------------------
        # Optional per-role overrides of USER_RATE_LIMIT_PER_MINUTE, as `role:limit` pairs
        # ("admin:60,vendor:20,customer:10"). An operator answering a support question and a
        # customer poking at a chat widget are not the same load, and one flat number has to be
        # set for the worst of them. Empty means every role shares the flat limit.
        #
        # USER_RATE_LIMIT_PER_MINUTE=0 still disables rate limiting entirely -- it stays the
        # master switch, so these overrides can never turn limiting back on where it was meant
        # to be off.
        self.rate_limit_by_role = _parse_role_map(
            "RATE_LIMIT_BY_ROLE", os.environ.get("RATE_LIMIT_BY_ROLE", "")
        )
        # Questions one identity may ask per UTC day. Distinct from GEMINI_DAILY_CALL_BUDGET,
        # which is a *shared* ceiling in Gemini calls: this bounds one principal in questions, so
        # a single vendor cannot spend the whole workspace's free-tier quota before anyone else
        # gets a turn. The sliding-window rate limit stops a burst; this stops a slow drain.
        # 0 disables it.
        self.user_daily_question_limit = _int("USER_DAILY_QUESTION_LIMIT", "0")
        # Per-role row caps for a generated report, same `role:limit` shape. REPORT_MAX_ROWS stays
        # the absolute ceiling. Rendering is the most expensive CPU on the request path and runs
        # on a bounded pool, so "how many rows may this role ask WeasyPrint for" is a real load
        # question once customers can reach it.
        self.report_max_rows_by_role = _parse_role_map(
            "REPORT_MAX_ROWS_BY_ROLE", os.environ.get("REPORT_MAX_ROWS_BY_ROLE", "")
        )
        # Whether a generated report may carry contact columns for rows the principal is already
        # authorized to see (app/db/identity.py). Off by default: exposing contact details is a
        # deliberate act, and the safe default for a field the model is otherwise never shown is
        # to keep it that way. See docs/authorization.md.
        self.report_include_contacts = (
            os.environ.get("REPORT_INCLUDE_CONTACTS", "false").strip().lower() == "true"
        )

        # Serves a browser-usable playground at the gateway's `/`, where a session can be minted
        # WITHOUT a host-app key -- i.e. a deliberate hole in the one credential boundary this
        # service owns. Off by default, refused for any non-loopback caller even when on, and
        # announced loudly at startup. It exists because the alternative is developers pasting
        # tokens around by hand, which is worse. The landing page itself is always served; only
        # the minting is gated.
        self.widget_dev_playground = (
            os.environ.get("WIDGET_DEV_PLAYGROUND", "false").strip().lower() == "true"
        )

        # --- Shared state (app/state/) ------------------------------------------------------
        # Where the stateful guardrails keep their state: the answer cache, the three
        # per-conversation caches, the rate limiter, the daily Gemini budget, the circuit
        # breaker, the web adapter's report store, and the `/login` vendor sessions.
        #
        # "memory" (default) is the original in-process behaviour and needs no extra service --
        # correct, fast, and single-replica by construction. "redis" is what makes running more
        # than one instance actually correct rather than merely possible: without it each replica
        # enforces its own daily budget and its own rate limit, so N replicas admit N times the
        # traffic those limits were set to allow, and a follow-up question answered by a
        # different replica loses its context.
        self.state_backend = os.environ.get("STATE_BACKEND", "memory").strip().lower()
        self.redis_url = os.environ.get("REDIS_URL", "")
        # Namespaces every key this app writes, so it can share a Redis with something else and
        # so `clear()` can scope itself instead of reaching for FLUSHDB.
        self.state_key_prefix = os.environ.get("STATE_KEY_PREFIX", "ragchat")

        # --- Web chat plugin (app/api/) -----------------------------------------------------
        # The embeddable chatbot gateway is a *second adapter* onto the same pipeline the Slack
        # handlers use, so none of this is required to run the Slack bot -- these stay optional
        # at import time and are validated by widget_config_error() when the API actually starts.
        # _require()ing them here would break every existing Slack-only deployment (and CI, which
        # supplies exactly the six variables app/main.py needs) the moment this module imported.
        #
        # Host-app server keys, comma-separated. Each entry is `tenant_id:secret` or a bare
        # `secret` (tenant then defaults to "default"). A list rather than a single value so a key
        # can be rotated without downtime -- add the new one, redeploy hosts, drop the old one --
        # and so one gateway can serve several host applications, which is the direction this
        # goes. These are *server-side* keys: a browser must never hold one (see app/api/tokens.py).
        self.widget_api_keys = _parse_list(os.environ.get("WIDGET_API_KEYS", ""))
        # HS256 signing key for the short-lived session tokens the browser does hold. Separate
        # from widget_api_keys on purpose: rotating a host app's key must not invalidate every
        # live chat session, and leaking one must not let the holder mint the other.
        self.widget_jwt_secret = os.environ.get("WIDGET_JWT_SECRET", "")
        # How long a browser session token is good for, and therefore the worst-case delay
        # before a suspended or re-scoped account stops being answered.
        #
        # This *is* the revocation mechanism. A JWT cannot be withdrawn, but every renewal goes
        # back through POST /v1/session, which re-checks the account against `users`
        # (widget_verify_asserted_identity below) -- so revocation propagates within one TTL with
        # no revocation list to maintain. 15 minutes costs an indexed lookup four times an hour
        # per active user; lower it and revocation tightens proportionally.
        self.widget_session_ttl_seconds = _int("WIDGET_SESSION_TTL_SECONDS", "900")
        # A generated CSV/XLSX/PDF is held in memory just long enough for the browser to fetch
        # it. Slack took the bytes inline (files_upload_v2); a browser needs a URL, and a URL
        # that outlives the answer is an unbounded store of query results nobody is watching.
        self.widget_file_ttl_seconds = _int("WIDGET_FILE_TTL_SECONDS", "600")
        self.widget_max_cached_files = _int("WIDGET_MAX_CACHED_FILES", "200")
        # Browser origins allowed to call the gateway. Empty means unrestricted, matching the
        # convention used by MONGODB_ALLOWED_COLLECTIONS and SLACK_ALLOWED_CHANNEL_IDS -- see
        # widget_cors_warning(), which says so out loud at startup rather than leaving it implicit.
        self.widget_allowed_origins = _parse_list(os.environ.get("WIDGET_ALLOWED_ORIGINS", ""))
        # Defaults to loopback, not 0.0.0.0: binding every interface is a deployment decision
        # (behind a reverse proxy, in a container), not something a developer should get by
        # accident on a laptop.
        self.widget_api_host = os.environ.get("WIDGET_API_HOST", "127.0.0.1")
        self.widget_api_port = _int("WIDGET_API_PORT", "8000")
        # Bounds what a browser can post before anything downstream sees it. A Slack message is
        # capped by Slack; an HTTP body is capped by whoever wrote the server.
        self.widget_max_question_chars = _int("WIDGET_MAX_QUESTION_CHARS", "2000")
        # Which roles a host application's key may assert at POST /v1/session. `admin` is
        # deliberately NOT in the default: a host key is a long-lived secret sitting on someone
        # else's server, and the blast radius of leaking one should not include minting an
        # unrestricted session. Add it explicitly when an internal console genuinely needs it.
        self.widget_allowed_session_roles = _parse_list(
            os.environ.get("WIDGET_ALLOWED_SESSION_ROLES", "customer,vendor")
        )
        # Re-check an asserted vendor/customer against `users` when a session is minted: is this
        # account still there, and still active? The host application has just authenticated
        # them, so this is not about trusting it -- it is about offboarding. A suspended vendor
        # whose host app still has them logged in would otherwise keep reading until their token
        # expired, which is the hole that makes role-based access theatre.
        #
        # Costs one indexed lookup per sign-in, not per question. Turn it off only if the host
        # application's users legitimately have no row in `users` (admin accounts never do, and
        # are always skipped).
        self.widget_verify_asserted_identity = (
            os.environ.get("WIDGET_VERIFY_ASSERTED_IDENTITY", "true").strip().lower() == "true"
        )

    def rate_limit_override_for(self, role: str) -> int | None:
        """This role's per-window request limit, or None to use the configured flat one.

        None rather than "the flat value" on purpose: the caller passes it straight to
        `RateLimiter.allow`, where None means "use whatever this limiter was built with". Handing
        back a resolved number instead would make the *setting* authoritative over the limiter
        object, which quietly breaks anything that configured a limiter directly -- tests
        included.
        """
        return self.rate_limit_by_role.get(role)

    def report_max_rows_override_for(self, role: str) -> int | None:
        """This role's report row cap, or None for the configured REPORT_MAX_ROWS.

        `build_table` clamps whatever it gets to REPORT_MAX_ROWS, so an override can only lower
        the cap, never raise it past the bound the render pool was sized for.
        """
        return self.report_max_rows_by_role.get(role)

    def role_config_error(self) -> str | None:
        """None if SLACK_DEFAULT_ROLE names a real role, otherwise why it doesn't.

        Fatal rather than falling back, because both possible fallbacks are wrong in opposite
        directions: defaulting to admin on a typo grants everything, and defaulting to anonymous
        silently breaks every existing Slack user.
        """
        from app.security.roles import Role

        valid = {r.value for r in Role}
        if self.slack_default_role not in valid:
            return (
                f"SLACK_DEFAULT_ROLE is {self.slack_default_role!r}; it must be one of "
                f"{', '.join(sorted(valid))}."
            )
        return None

    def state_config_error(self) -> str | None:
        """None if the configured state backend can actually be built, otherwise why it can't.

        Checked at startup by both entrypoints. Falling back to in-process state when Redis was
        asked for would be the worst outcome available: the process starts, every request
        succeeds, and the limits silently stop being shared -- which is invisible until a budget
        is overrun by exactly the number of replicas running.
        """
        if self.state_backend not in ("memory", "redis"):
            return (
                f"STATE_BACKEND is {self.state_backend!r}; it must be 'memory' (in-process, "
                "single replica) or 'redis' (shared across replicas)."
            )
        if self.state_backend == "redis" and not self.redis_url:
            return "STATE_BACKEND is 'redis' but REDIS_URL is not set."
        return None

    def single_replica_warning(self) -> str | None:
        """None unless state is in-process, in which case a reminder of what that costs."""
        if self.state_backend != "memory":
            return None
        return (
            "STATE_BACKEND is 'memory', so the answer cache, rate limit, daily budget, "
            "conversation context and generated reports all live in this process only. Run "
            "exactly one instance, or set STATE_BACKEND=redis before scaling out."
        )

    def widget_config_error(self) -> str | None:
        """None if the web chat gateway has what it needs to start, otherwise why it doesn't.

        Fatal rather than a warning, and checked at API startup rather than at import: a gateway
        with no signing key would happily accept *any* token, and one with no host-app key would
        let anyone mint a session for any user with any data scope. Both are silent -- the server
        starts, requests succeed, and the scoping guardrail simply isn't there. Failing to boot is
        the only outcome that surfaces it.

        The placeholder check is what lets `.env.example` show these two as filled-in values
        rather than blanks. A sample that ships a *usable* signing key is strictly worse than one
        that ships none -- every deployment that copied it without reading would share a key
        printed in the repository, and nothing would say so. Rejecting the marker keeps the
        sample legible and copy-paste safe at the same time.
        """
        if not self.widget_jwt_secret:
            return (
                "WIDGET_JWT_SECRET is not set. The web chat gateway signs browser session tokens "
                "with it; without one, session tokens cannot be verified."
            )
        if _is_placeholder(self.widget_jwt_secret):
            return (
                "WIDGET_JWT_SECRET is still the placeholder from .env.example. Generate a real "
                "one with `python -c 'import secrets; print(secrets.token_urlsafe(48))'`."
            )
        if len(self.widget_jwt_secret) < 32:
            return (
                "WIDGET_JWT_SECRET is too short (need at least 32 characters). Generate one with "
                "`python -c 'import secrets; print(secrets.token_urlsafe(48))'`."
            )
        if not self.widget_api_keys:
            return (
                "WIDGET_API_KEYS is empty. Host applications authenticate to the session endpoint "
                "with one of these keys; with none set, no host application can mint a session."
            )
        if any(_is_placeholder(key) for key in self.widget_api_keys):
            return (
                "WIDGET_API_KEYS still contains the placeholder from .env.example. Replace it "
                "with a real secret -- anyone holding one can mint a session for any user."
            )
        return None

    def dev_playground_warning(self) -> str | None:
        """None unless the playground is on, in which case say exactly what it permits."""
        if not self.widget_dev_playground:
            return None
        return (
            "WIDGET_DEV_PLAYGROUND is on: any request from this machine can mint a chat session "
            "as any permitted role, with no host-app key. Loopback callers only, and never "
            "somewhere real."
        )

    def widget_cors_warning(self) -> str | None:
        """None if browser origins are restricted, otherwise a warning that they aren't.

        An empty allow-list means "open" here for consistency with the other allow-lists in this
        file, which is a reasonable default for a locally-run demo and a poor one in production.
        Saying so at startup is the difference between a deliberate choice and an unnoticed one.
        """
        if self.widget_allowed_origins:
            return None
        return (
            "WIDGET_ALLOWED_ORIGINS is empty, so the web chat gateway accepts requests from any "
            "browser origin. Set it to the origins that actually embed the widget."
        )

    def gemini_timeout_warning(self) -> str | None:
        """None unless `GEMINI_REQUEST_TIMEOUT_MS` was raised to the API's floor.

        Google rejects a deadline below 10s with `400 INVALID_ARGUMENT` -- not for the slow calls,
        for *every* call -- so a smaller value doesn't tighten latency, it stops the application
        working entirely. Clamping keeps it working; this says out loud that it happened, because
        a setting silently not being the number you typed is its own kind of bug.
        """
        if self._requested_gemini_timeout_ms >= GEMINI_MIN_REQUEST_TIMEOUT_MS:
            return None
        return (
            f"GEMINI_REQUEST_TIMEOUT_MS ({self._requested_gemini_timeout_ms}ms) is below the "
            f"{GEMINI_MIN_REQUEST_TIMEOUT_MS}ms minimum the Gemini API accepts -- it rejects a "
            "shorter deadline on every call with 400 INVALID_ARGUMENT. Using "
            f"{GEMINI_MIN_REQUEST_TIMEOUT_MS}ms instead; set it to at least that to silence this."
        )

    def retry_budget_warning(self) -> str | None:
        """None if a slow Gemini failure can actually be retried, otherwise a warning.

        `_call_with_retry` measures its ceiling from the start of the *first attempt*, so a
        ceiling at or below the per-attempt timeout silently disables retrying for exactly the
        failures that most need it: a `504 DEADLINE_EXCEEDED` and a client-side timeout are slow
        by construction. Both settings look reasonable in isolation, which is why this is checked
        out loud rather than left to whoever next reads two numbers in different units.

        A warning rather than a fatal error: the resulting behaviour is degraded, not unsafe, and
        an operator who deliberately wants one-shot calls should be able to have them.
        """
        timeout_seconds = self.gemini_request_timeout_ms / 1000
        if self.gemini_max_retry_seconds > timeout_seconds:
            return None
        return (
            f"GEMINI_MAX_RETRY_SECONDS ({self.gemini_max_retry_seconds}) is not greater than "
            f"GEMINI_REQUEST_TIMEOUT_MS ({self.gemini_request_timeout_ms}ms = "
            f"{timeout_seconds}s), so a slow failure -- a 504, or the timeout itself -- can never "
            "be retried: the first attempt spends the whole budget before a retry is considered. "
            f"Raise GEMINI_MAX_RETRY_SECONDS above {timeout_seconds}s (twice it, to fit a second "
            "attempt), or lower GEMINI_REQUEST_TIMEOUT_MS."
        )

    def pool_size_warning(self) -> str | None:
        """None if mongodb_max_pool_size comfortably covers worst-case concurrent DB usage,
        otherwise a human-readable warning. Worst case is every Socket Mode worker thread
        simultaneously running a question that fans out to agent_max_fan_out parallel domain
        queries -- if the pool is smaller than that, requests start queuing on pool checkout,
        silently reintroducing the latency these timeouts were meant to bound. Checked explicitly
        at startup (see app/main.py) rather than asserted in __init__, since a too-small pool is a
        performance warning, not something that should crash the process.

        The `+ 1` covers app/agents/graph.py::_resolve_anchors_node, which issues its own
        customer/vendor lookups *before* the fan-out on cross-domain geo questions. Sizing the
        pool at exactly concurrency * fan_out left zero headroom for those, so the worst case
        genuinely exceeded the pool and checkouts queued silently."""
        required = self.slack_socket_mode_concurrency * (self.agent_max_fan_out + 1)
        if self.mongodb_max_pool_size >= required:
            return None
        return (
            f"MONGODB_MAX_POOL_SIZE ({self.mongodb_max_pool_size}) is below "
            f"SLACK_SOCKET_MODE_CONCURRENCY * (AGENT_MAX_FAN_OUT + 1) ({required}) -- DB "
            "connections "
            "may become the concurrency bottleneck under load. Raise MONGODB_MAX_POOL_SIZE to "
            f"at least {required}."
        )


settings = Settings()
