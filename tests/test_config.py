import pytest

from app.config import Settings

_REQUIRED_ENV = {
    "SLACK_BOT_TOKEN": "xoxb-test",
    "SLACK_APP_TOKEN": "xapp-test",
    "SLACK_SIGNING_SECRET": "secret",
    "GEMINI_API_KEY": "test-key",
    "MONGODB_URI": "mongodb://localhost",
    "MONGODB_DB_NAME": "testdb",
}


def _set_required_env(monkeypatch):
    for name, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)


def test_missing_required_env_var_names_itself_in_the_error(monkeypatch):
    for name in _REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    _set_required_env(monkeypatch)
    monkeypatch.delenv("MONGODB_DB_NAME", raising=False)

    with pytest.raises(RuntimeError, match="MONGODB_DB_NAME"):
        Settings()


def test_invalid_int_env_var_names_itself_and_the_bad_value(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("GEMINI_MAX_RETRIES", "three")

    with pytest.raises(RuntimeError, match="GEMINI_MAX_RETRIES.*'three'"):
        Settings()


def test_invalid_float_env_var_names_itself_and_the_bad_value(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("GEMINI_RETRY_BASE_DELAY_SECONDS", "soon")

    with pytest.raises(RuntimeError, match="GEMINI_RETRY_BASE_DELAY_SECONDS.*'soon'"):
        Settings()


def test_defaults_are_sane(monkeypatch):
    _set_required_env(monkeypatch)
    for name in (
        "USER_RATE_LIMIT_PER_MINUTE",
        "GEMINI_CIRCUIT_BREAKER_THRESHOLD",
        "GEMINI_CIRCUIT_BREAKER_COOLDOWN_SECONDS",
        "AUDIT_LOG_FILE",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings()

    # Enabled by default: one question costs several Gemini calls against a free-tier daily
    # quota, so an unbounded user can exhaust the whole workspace's budget on their own.
    assert settings.user_rate_limit_per_minute == 10
    assert settings.gemini_circuit_breaker_threshold == 5
    assert settings.gemini_circuit_breaker_cooldown_seconds == 30.0
    assert settings.audit_log_file == ""


def test_pool_size_warning_none_when_pool_covers_worst_case(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("SLACK_SOCKET_MODE_CONCURRENCY", "10")
    monkeypatch.setenv("AGENT_MAX_FAN_OUT", "3")
    # concurrency * (fan_out + 1) -- the +1 is the anchor-resolution query that runs ahead of
    # the fan-out on cross-domain geo questions.
    monkeypatch.setenv("MONGODB_MAX_POOL_SIZE", "40")

    assert Settings().pool_size_warning() is None


def test_pool_size_warning_accounts_for_anchor_resolution_queries(monkeypatch):
    """A pool sized at exactly concurrency * fan_out left no headroom for the anchor-resolution
    queries _resolve_anchors_node issues before the fan-out, so the real worst case exceeded the
    pool and checkouts queued silently."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("SLACK_SOCKET_MODE_CONCURRENCY", "10")
    monkeypatch.setenv("AGENT_MAX_FAN_OUT", "3")
    monkeypatch.setenv("MONGODB_MAX_POOL_SIZE", "30")

    warning = Settings().pool_size_warning()

    assert warning is not None
    assert "40" in warning


def test_pool_size_warning_flags_an_undersized_pool(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("SLACK_SOCKET_MODE_CONCURRENCY", "10")
    monkeypatch.setenv("AGENT_MAX_FAN_OUT", "3")
    monkeypatch.setenv("MONGODB_MAX_POOL_SIZE", "5")

    warning = Settings().pool_size_warning()

    assert warning is not None
    assert "MONGODB_MAX_POOL_SIZE" in warning
    assert "40" in warning


def test_a_timeout_below_the_api_floor_is_raised_to_it(monkeypatch):
    """Google rejects a deadline under 10s with 400 INVALID_ARGUMENT -- on *every* call, not just
    slow ones. An 8s default shipped and stopped the bot answering anything at all, so the value
    is clamped rather than trusted: a number the upstream refuses outright is not a preference."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("GEMINI_REQUEST_TIMEOUT_MS", "8000")

    settings = Settings()

    assert settings.gemini_request_timeout_ms == 10000
    warning = settings.gemini_timeout_warning()
    assert warning is not None
    assert "8000" in warning and "10000" in warning


def test_a_timeout_at_or_above_the_floor_is_left_alone(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("GEMINI_REQUEST_TIMEOUT_MS", "20000")

    settings = Settings()

    assert settings.gemini_request_timeout_ms == 20000
    assert settings.gemini_timeout_warning() is None


def test_the_shipped_timeout_is_one_the_api_accepts(monkeypatch):
    """The default is the thing most deployments run, so it is the one worth asserting."""
    _set_required_env(monkeypatch)
    monkeypatch.delenv("GEMINI_REQUEST_TIMEOUT_MS", raising=False)

    settings = Settings()

    assert settings.gemini_request_timeout_ms >= 10000
    assert settings.gemini_timeout_warning() is None


def test_the_shipped_retry_budget_leaves_room_for_a_retry(monkeypatch):
    """The defaults must agree with each other. They didn't: a 15s request timeout under an 8s
    retry budget meant the first attempt spent the budget, so a 504 and a client-side timeout --
    the two failures the retryable set exists for -- were never actually retried."""
    _set_required_env(monkeypatch)
    # Explicitly unset, or this reads the developer's own .env and tests their machine rather
    # than the shipped defaults -- which is how a .env still carrying the old pair would pass.
    monkeypatch.delenv("GEMINI_REQUEST_TIMEOUT_MS", raising=False)
    monkeypatch.delenv("GEMINI_MAX_RETRY_SECONDS", raising=False)

    assert Settings().retry_budget_warning() is None


def test_a_retry_budget_below_the_attempt_timeout_is_flagged(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("GEMINI_REQUEST_TIMEOUT_MS", "15000")
    monkeypatch.setenv("GEMINI_MAX_RETRY_SECONDS", "8.0")

    warning = Settings().retry_budget_warning()

    assert warning is not None
    assert "GEMINI_MAX_RETRY_SECONDS" in warning
    assert "GEMINI_REQUEST_TIMEOUT_MS" in warning


def test_a_retry_budget_equal_to_the_attempt_timeout_is_flagged(monkeypatch):
    """Equal is still useless: the attempt consumes the budget exactly, so the backoff can never
    fit inside what's left."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("GEMINI_REQUEST_TIMEOUT_MS", "8000")
    monkeypatch.setenv("GEMINI_MAX_RETRY_SECONDS", "8.0")

    assert Settings().retry_budget_warning() is not None


def test_every_documented_setting_is_actually_set():
    """Guards a whole class of bug that nothing else here catches.

    `Settings.__init__` is one long constructor, and a method definition accidentally landing in
    the middle of it silently turns every assignment below into unreachable code after a `return`.
    That happened: the widget settings became dead code and `settings.widget_file_ttl_seconds`
    stopped existing, with no syntax error, no lint warning, and nothing failing until an
    unrelated import blew up.

    Listing the names here is dull and that is the point -- a missing attribute is caught at the
    source rather than wherever it happens to be read first.
    """
    from app.config import settings

    expected = [
        # credentials / connection
        "slack_bot_token",
        "slack_app_token",
        "slack_signing_secret",
        "gemini_api_key",
        "mongodb_uri",
        "mongodb_db_name",
        "mongodb_allowed_collections",
        # mongo tuning
        "mongodb_query_timeout_ms",
        "mongodb_max_result_limit",
        "mongodb_max_pool_size",
        "mongodb_server_selection_timeout_ms",
        "mongodb_connect_timeout_ms",
        "mongodb_socket_timeout_ms",
        "mongodb_max_geo_radius_m",
        # gemini
        "gemini_model",
        "gemini_fallback_models",
        "gemini_max_retries",
        "gemini_max_retry_seconds",
        "gemini_request_timeout_ms",
        "gemini_answer_max_rows",
        "gemini_daily_call_budget",
        "gemini_circuit_breaker_threshold",
        "gemini_circuit_breaker_cooldown_seconds",
        # agent / caches / limits
        "agent_classifier_min_confidence",
        "agent_max_fan_out",
        "agent_max_clarification_rounds",
        "answer_cache_ttl_seconds",
        "answer_cache_max_entries",
        "clarification_cache_ttl_seconds",
        "conversation_context_ttl_seconds",
        "context_switch_confirmation_ttl_seconds",
        "user_rate_limit_per_minute",
        "user_rate_limit_window_seconds",
        # reports / security / slack
        "report_max_rows",
        "report_max_columns",
        "report_render_concurrency",
        "report_title",
        "security_extra_denied_fields",
        "vendor_session_ttl_seconds",
        "slack_allowed_channel_ids",
        "slack_allowed_user_ids",
        "slack_socket_mode_concurrency",
        # shared state (app/state/)
        "state_backend",
        "redis_url",
        "state_key_prefix",
        # web chat plugin (app/api/)
        "widget_api_keys",
        "widget_jwt_secret",
        "widget_session_ttl_seconds",
        "widget_file_ttl_seconds",
        "widget_max_cached_files",
        "widget_allowed_origins",
        "widget_api_host",
        "widget_api_port",
        "widget_max_question_chars",
    ]

    missing = [name for name in expected if not hasattr(settings, name)]
    assert not missing, f"Settings is missing: {missing}"


def test_the_documented_pool_size_default_is_the_one_in_effect():
    """The Mongo connection settings were once assigned twice, and the second (bare `int()`) copy
    silently won -- undoing the documented 40 and the helpful error messages `_int()` exists for.
    Nothing failed; the pool was simply half the size the comment above it explains."""
    from app.config import Settings

    settings = Settings()

    assert settings.mongodb_max_pool_size == 40
    assert settings.pool_size_warning() is None


def test_a_redis_backend_without_a_url_refuses_to_start(monkeypatch):
    """Falling back to in-process state when Redis was asked for is the worst outcome available:
    the process starts, every request succeeds, and the limits silently stop being shared."""
    from app.config import settings

    monkeypatch.setattr(settings, "state_backend", "redis")
    monkeypatch.setattr(settings, "redis_url", "")

    assert "REDIS_URL" in settings.state_config_error()


def test_an_unknown_state_backend_is_refused(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "state_backend", "memcached")

    assert "STATE_BACKEND" in settings.state_config_error()


def test_in_process_state_warns_about_what_it_costs(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "state_backend", "memory")

    assert "one instance" in settings.single_replica_warning()


def test_a_real_widget_secret_and_key_pass(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "widget_jwt_secret", "s" * 48)
    monkeypatch.setattr(settings, "widget_api_keys", ["tenant:a-real-secret"])

    assert settings.widget_config_error() is None


def test_the_env_example_jwt_placeholder_is_refused(monkeypatch):
    """The sample fills these in so the shape is visible; the marker is what keeps a value nobody
    replaced from becoming a live signing key shared by every deployment that copied the file."""
    from app.config import settings

    monkeypatch.setattr(settings, "widget_jwt_secret", "CHANGE_ME-paste-the-generated-value-here")
    monkeypatch.setattr(settings, "widget_api_keys", ["tenant:a-real-secret"])

    assert "WIDGET_JWT_SECRET" in settings.widget_config_error()


def test_the_env_example_api_key_placeholder_is_refused(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "widget_jwt_secret", "s" * 48)
    monkeypatch.setattr(
        settings, "widget_api_keys", ["default:CHANGE_ME-shared-secret-your-host-app-sends"]
    )

    assert "WIDGET_API_KEYS" in settings.widget_config_error()


def test_the_placeholder_check_ignores_case(monkeypatch):
    """A placeholder someone lower-cased while editing is still a placeholder."""
    from app.config import settings

    monkeypatch.setattr(settings, "widget_jwt_secret", "change_me-" + "x" * 40)
    monkeypatch.setattr(settings, "widget_api_keys", ["tenant:a-real-secret"])

    assert "WIDGET_JWT_SECRET" in settings.widget_config_error()
