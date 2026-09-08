# Slack → MongoDB RAG Assistant

Ask questions in Slack, get answers computed from your MongoDB data via Google's Gemini (free tier).

## How it works

1. A user @-mentions the bot, DMs it, or runs `/ask <question>` in Slack.
2. The question is **classified** into one or more domains (`orders`/`customers`/`vendors`); a
   question needing a cross-domain geo lookup (e.g. "vendors near customer X with pending
   orders") first resolves that anchor in code (never by asking the model to invent coordinates).
   A short follow-up in the same conversation (e.g. "what about the total amount?" right after
   "how many orders did vendor V1 have last week?") is recognized at this same step and folded
   into one self-contained question (see
   [docs/architecture.md](docs/architecture.md#the-agent-graph-in-detail)). If a question instead
   looks *unrelated* to what was just asked, the bot doesn't guess either way — it asks "should I
   clear that context and answer this as a new question?" and waits for a yes/no before generating
   anything, so it only ever discards context you actually confirmed dropping. Say `reset` (or
   `new topic` / `start over` / `forget that`) at any time to clear that context immediately
   without waiting to be asked.
3. The question fans out to one schema-scoped **domain agent** per classified domain (in
   parallel), each producing a structured query (`{collection, operation, filter/pipeline, ...}`)
   — not raw code — scoped so it can only ever touch its own domain's data, regardless of what
   the model itself writes.
4. Each query is validated (allowed collection only, no destructive/JS operators, result limit
   and date-range/geo-radius capped) before it ever touches the database, then run read-only
   against MongoDB.
5. Any math (totals, averages, etc.) is pushed into the MongoDB aggregation pipeline as part of
   the generated query, or reasoned over directly by Gemini when writing the final answer.
6. Gemini synthesizes the rows from every domain into one concise natural-language reply, posted
   back to Slack.

If a question can't be answered from the available data, or is ambiguous, the bot asks a
clarifying question instead of guessing.

See [docs/architecture.md](docs/architecture.md) for the full component map, the multi-domain
agent graph, and sequence diagrams of both entry points (`/ask` and `@mention`/DM), and
[docs/onboarding-a-collection.md](docs/onboarding-a-collection.md) for adding new data.

## Project layout

```
app/
  config.py              # loads and validates .env settings
  agents/                 # multi-domain LangGraph agent (classify -> resolve anchors -> fan out -> synthesize)
    classifier.py          # question -> domain(s) + confidence + clarification + context_mode/resolved_question
    domains.py              # domain registry; forces collection/usertype scoping in code
    graph.py                 # the StateGraph itself (app/agents/state.py holds its schema)
    query_agents.py           # per-domain schema-scoped query generation
  db/
    mongo.py               # MongoDB client/connection (pooled, timeouts)
    indexes.py               # idempotent index creation for the fields agents filter on
    introspect.py             # samples collections -> schema_summary.json
    executor.py                # runs a validated QuerySpec against MongoDB
    seed.py, seed_users.py       # demo/sample data generators
  rag/
    query_spec.py           # QuerySpec / QueryError pydantic models
    validator.py             # safety checks on LLM-generated queries
    schema_context.py         # schema_summary.json (+ annotations) -> prompt text
    calculation.py             # pure total/average/min/max/count helpers
    pipeline.py                 # orchestrates: cache -> rate limit -> budget -> graph -> audit log
    answer_cache.py               # per-channel TTL+LRU cache of full answers
    clarification_cache.py         # per-(channel,user) "we asked a follow-up" cache
    conversation_context.py         # per-(channel,user) last resolved_question, for short follow-ups
    context_switch_cache.py         # per-(channel,user) "should I clear that context?" pending confirmation
    rate_limiter.py                 # per-(channel,user) sliding-window rate limit
  llm/
    gemini_client.py        # Gemini calls: retry/backoff/timeout/thinking-budget/quota
    circuit_breaker.py        # fails fast on a sustained Gemini outage instead of retrying forever
    quota.py                    # shared daily call budget
  slack/
    handlers.py              # app_mention / DM / `/ask` handlers
    access_control.py          # channel/user allowlists
  audit/
    logger.py                 # structured JSON audit logging (stdout + optional file)
  main.py                    # Slack Socket Mode entrypoint
tests/                      # pytest suite, fully mocked (no live Slack/Mongo/Gemini calls)
```

See [CLAUDE.md](CLAUDE.md) for a fuller map of how these pieces fit together, the invariants each
one enforces, and the tradeoffs behind them.

## Setup

1. **Python environment**

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt        # runtime only
   pip install -r requirements-dev.txt    # + pytest, for running tests
   ```

2. **Configure secrets** — copy `.env.example` to `.env` and fill in:
   - `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_SIGNING_SECRET` — from your Slack app
     (api.slack.com/apps), with Socket Mode + Events API (app mentions, DMs) and the `/ask`
     slash command enabled.
   - `GEMINI_API_KEY` — from https://aistudio.google.com/apikey (free tier).
   - `MONGODB_URI`, `MONGODB_DB_NAME`, `MONGODB_ALLOWED_COLLECTIONS` — connection info and the
     collections the bot is allowed to query. Use a **read-only** database user for this.

   Optional tuning (defaults shown):
   - `GEMINI_MODEL=gemini-3-flash-preview`
   - `MONGODB_QUERY_TIMEOUT_MS=8000`
   - `MONGODB_MAX_RESULT_LIMIT=200`
   - `AUDIT_LOG_LEVEL=INFO`
   - `GEMINI_MAX_RETRIES=3`, `GEMINI_RETRY_BASE_DELAY_SECONDS=1.0` -- retries now also cover
     client-side HTTP timeouts, not just Gemini-returned 429/5xx responses.
   - `GEMINI_MAX_RETRY_SECONDS=8.0` -- hard wall-clock ceiling on retry backoff, independent of
     `GEMINI_MAX_RETRIES`, so a question can't stall indefinitely on repeated transient errors.
     Note this is a ceiling *per call*, and one question makes several (classify, per-domain
     fan-out, synthesize) -- at the old 20.0 a single question could sit in backoff for over a
     minute while holding a Socket Mode worker thread.
   - `GEMINI_REQUEST_TIMEOUT_MS=15000` -- client-side HTTP timeout per Gemini call. If this fires
     it's now retried (see above) instead of failing the question outright on the first slow
     response.
   - `GEMINI_QUERY_THINKING_BUDGET=0` -- disables "thinking" for the query-generation call (0 =
     off, -1 = automatic). Query generation is deterministic structured extraction, not open-ended
     reasoning; on thinking-capable models this both removes needless latency and avoids a real
     failure mode we hit in production, where reasoning tokens silently consumed the output-token
     budget and truncated the JSON mid-object before the model ever reached the closing brace.
   - `GEMINI_QUERY_MAX_OUTPUT_TOKENS=2048` -- output-token cap for the query-generation call only.
     Kept generous specifically because a too-small cap is what caused the truncation above --
     this is a safety ceiling, not the latency lever (`GEMINI_QUERY_THINKING_BUDGET` is).
   - `GEMINI_ANSWER_MAX_ROWS=30` -- caps how many result rows are serialized into the
     answer-generation prompt, independent of `MONGODB_MAX_RESULT_LIMIT`, so a large result set
     doesn't inflate prompt size (and Gemini latency).
   - `GEMINI_DAILY_CALL_BUDGET=0` (0 = unlimited)
   - `MONGODB_SERVER_SELECTION_TIMEOUT_MS=3000`, `MONGODB_CONNECT_TIMEOUT_MS=3000`,
     `MONGODB_SOCKET_TIMEOUT_MS=8000` -- pymongo's own default for server selection is 30s; left
     unset, a slow/unreachable Mongo can silently eat up to 30s of a request before the query even
     starts. These give it an explicit, much lower ceiling.
   - `MONGODB_MAX_POOL_SIZE=40` -- should stay at or above
     `SLACK_SOCKET_MODE_CONCURRENCY * (AGENT_MAX_FAN_OUT + 1)` or DB connections become the
     concurrency bottleneck. Worst case is every worker thread running a question that fans out
     to every domain agent at once; the `+ 1` covers the anchor-resolution queries
     `_resolve_anchors_node` issues *before* the fan-out on cross-domain geo questions, which the
     old `concurrency * fan_out` sizing left no headroom for. `app.main` checks this relationship
     at startup and logs a warning if it doesn't hold (see `Settings.pool_size_warning` in
     [app/config.py](app/config.py)).
   - `SLACK_SOCKET_MODE_CONCURRENCY=10` -- thread pool size for the Socket Mode client (this is
     `slack_sdk`'s own default; made explicit here so it's tuned deliberately, not left implicit).
   - `SLACK_ALLOWED_CHANNEL_IDS`, `SLACK_ALLOWED_USER_IDS` (comma-separated; empty = open to all)

   **Required bot token scopes.** Under *OAuth & Permissions -> Bot Token Scopes* in your Slack
   app, the token needs at least `chat:write` (posting answers), `files:write` (attaching
   generated CSV/XLSX/PDF reports), `commands` (the slash commands), plus `app_mentions:read`
   and `im:history` for the mention and DM event subscriptions. **A scope change only takes
   effect once you reinstall the app to the workspace**, which issues a new `SLACK_BOT_TOKEN`.

   Missing `files:write` is the one that degrades quietly: every text answer keeps working and
   only report requests fall back to prose, with a logged `slack_file_upload_failed`. `app.main`
   checks the granted scopes at startup and logs `startup_scope_warning` naming what's missing,
   so this surfaces at boot rather than the first time somebody asks for a PDF.
   - `USER_RATE_LIMIT_PER_MINUTE=10` (0 = disabled), `USER_RATE_LIMIT_WINDOW_SECONDS=60.0` -- caps
     how many questions one (channel, user) pair may ask per window
     ([app/rag/rate_limiter.py](app/rag/rate_limiter.py)). Independent of
     `GEMINI_DAILY_CALL_BUDGET`, which is a *shared* ceiling across every user -- this stops one
     chatty user from consuming that shared budget (or the underlying Gemini free-tier quota)
     alone before anyone else gets a turn. **On by default** (it previously defaulted to
     disabled): one question costs several Gemini calls against a free-tier daily quota measured
     in tens, so an unbounded user can exhaust the whole workspace's budget in under a minute.

   Report generation ([app/generators/](app/generators/)):
   - `REPORT_MAX_ROWS=1000`, `REPORT_MAX_COLUMNS=12` -- caps per generated table. Bounds render
     cost and file size, and keeps a wide Mongo document from rendering an unreadable table.
     Whenever a cap actually bites, the file says so explicitly rather than truncating silently.
   - `REPORT_MAX_PIE_SLICES=8` -- above this many categories a pie becomes a bar chart.
   - `REPORT_RENDER_CONCURRENCY=2` -- bounded pool for PDF/XLSX rendering, so document rendering
     can't saturate every Socket Mode worker at once (WeasyPrint is the heaviest CPU on the
     request path).
   - `REPORT_RENDER_TIMEOUT_SECONDS=25.0` -- wall-clock ceiling on one render; past it the user
     gets the text answer rather than waiting indefinitely.
   - `REPORT_TITLE="Data Report"` -- title shown on generated documents.

   Security ([app/security/](app/security/)):
   - `SECURITY_EXTRA_DENIED_FIELDS` (comma-separated, empty by default) -- extra field names to
     drop from every row, on top of the built-in internal/secret patterns in
     [app/security/field_policy.py](app/security/field_policy.py).
   - `VENDOR_SESSION_TTL_SECONDS=3600` -- how long a `/login` vendor session stays valid. Bounded
     so an abandoned session can't keep a scoped identity alive indefinitely in a long-lived
     process (a stale identity silently changes which rows a question returns).
   - `GEMINI_CIRCUIT_BREAKER_THRESHOLD=5`, `GEMINI_CIRCUIT_BREAKER_COOLDOWN_SECONDS=30.0` (0
     threshold disables it) -- after this many *consecutive* Gemini failures, every call fails
     immediately for the cooldown period instead of paying the full retry/timeout cost per
     request, so a sustained Gemini outage can't tie up the entire Socket Mode thread pool
     ([app/llm/circuit_breaker.py](app/llm/circuit_breaker.py)).
   - `AUDIT_LOG_FILE=` (empty = stdout only) -- if set, audit events are also written to this
     path via a rotating file handler (`AUDIT_LOG_FILE_MAX_BYTES=10485760`,
     `AUDIT_LOG_FILE_BACKUP_COUNT=5`), so they survive a container restart instead of only
     existing in however stdout happens to be captured
     ([app/audit/logger.py](app/audit/logger.py)).
   - `CONVERSATION_CONTEXT_TTL_SECONDS=300` -- how long a (channel, user)'s last resolved question
     stays available for the classifier to fold a short follow-up into (e.g. "what about the total
     amount?"). Same-session, single-turn only, never a growing transcript
     ([app/rag/conversation_context.py](app/rag/conversation_context.py)) -- not a substitute for
     long-term/cross-session memory, which is out of scope for now (see
     [CONTRIBUTING.md](CONTRIBUTING.md)).
   - `CONTEXT_SWITCH_CONFIRMATION_TTL_SECONDS=120` -- how long the "should I clear that context and
     answer this as a new question?" prompt stays valid before a later message is treated as an
     ordinary fresh question instead of a reply to it
     ([app/rag/context_switch_cache.py](app/rag/context_switch_cache.py)). The reply itself (and
     the `reset`/`new topic`/`start over`/`forget that` commands) are matched by exact phrase, not
     sent to Gemini.

3. **Generate a schema summary** so Gemini knows your data's shape:

   ```bash
   python -m app.db.introspect
   ```

   This samples documents from each allowed collection and writes `schema_summary.json`.
   Create an optional `schema_annotations.json` (git-ignored) alongside it to add human context
   per field, e.g.:

   ```json
   { "orders": { "total_amount": "order total in USD, tax included" } }
   ```

   Review both files before trusting the bot's query generation — the LLM only knows what's in
   these files plus field names/types/examples from the raw data.

4. **Run the bot**

   ```bash
   python -m app.main
   ```

   Socket Mode means no public URL or tunnel is needed for local development.

## Running in a container

This is a build/run recipe, not a chosen hosting target — where to actually deploy it is a
separate decision. [Dockerfile](Dockerfile) builds a minimal image (`python:3.12-slim`, runtime
dependencies only, non-root user, no exposed port since Socket Mode needs no inbound connection).

```bash
docker build -t 10xdev .
docker run --rm \
  --env-file .env \
  -v "$(pwd)/schema_summary.json:/app/schema_summary.json:ro" \
  -v "$(pwd)/schema_annotations.json:/app/schema_annotations.json:ro" \
  10xdev
```

`schema_summary.json`/`schema_annotations.json` are environment-specific and gitignored, so
they're mounted at runtime rather than baked into the image — keeps the image reusable across
environments. Regenerate `schema_summary.json` (`python -m app.db.introspect`) on the host before
running the container, same as local development.

## Testing

```bash
pytest
```

The suite covers the query safety validator (banned operators, disallowed collections, $lookup
cross-collection checks, limit/date-range/geo-radius clamping), the calculation helpers,
schema-context building and caching, schema introspection's type/example logic, the multi-domain
agent graph (classification routing, cross-domain anchor resolution and its Gemini-call dedup,
per-domain fan-out, clarification round-trips), the end-to-end pipeline orchestration (including
per-stage timing, the answer/clarification caches, the per-user rate limiter, and the daily call
budget), Gemini retry/deadline/circuit-breaker behavior (including client-side timeout retries)
and answer-row truncation, Mongo client connection config and lifecycle, DB index bootstrapping,
config env-var validation, Slack handler wiring (verifying the injected `GeminiClient` reaches
every entry point), and startup/shutdown wiring in `app.main` — all with mocked
Gemini/MongoDB/Slack, so `pytest` never makes network calls or needs real credentials.

## CI/CD

[.github/workflows/ci.yml](.github/workflows/ci.yml) runs on every push/PR to `main`:

| Job | Tool | Checks |
|---|---|---|
| Tests | pytest | full test suite |
| Lint & format | ruff | lint rules + formatting |
| SAST | bandit | Python security anti-patterns |
| Dependency audit | pip-audit | known CVEs in dependencies |
| Secret scan | gitleaks | hardcoded keys/tokens in git history |

All five are separate jobs so they can each be set as a required status check in the repo's
branch protection settings (GitHub setting, not part of the workflow file itself).

### Branch protection setup

On GitHub: **Settings → Branches → Add branch protection rule** (target `main`):

1. Check **Require a pull request before merging**.
2. Check **Require status checks to pass before merging**, then search for and add all five:
   `Tests`, `Lint & format`, `Static security analysis (bandit)`,
   `Dependency vulnerability audit`, `Secret scan (gitleaks)`.
3. Check **Require branches to be up to date before merging**.
4. Leave force-push and branch-deletion allowances **unchecked**.

The five checks only appear in the search box after they've run at least once on the repo (e.g.
after the first PR), since GitHub discovers job names from workflow runs.

## Pre-commit hooks

The same checks run locally before you can even push, so issues are caught before CI:

1. Install gitleaks (one-time, used by the secret-scan hook):

   ```bash
   curl -sSL https://github.com/gitleaks/gitleaks/releases/download/v8.21.2/gitleaks_8.21.2_linux_x64.tar.gz \
     | tar -xz gitleaks
   mkdir -p ~/.local/bin && mv gitleaks ~/.local/bin/
   ```

2. Install and register the hooks (uses `requirements-dev.txt`, already installed in step 1 of
   Setup):

   ```bash
   pre-commit install --install-hooks -t pre-commit -t pre-push
   ```

**On every `git commit`** (fast, local-scope checks): trailing whitespace / EOF / merge-conflict
markers / large-file guard, JSON/TOML/YAML syntax, private-key detection, ruff lint+format
(auto-fixes when possible), bandit, and gitleaks against the staged diff.

**On every `git push`** (heavier, broader-scope checks): the full pytest suite and pip-audit —
kept out of the commit path since pip-audit needs a network call and both are better suited to
running once per push than once per commit.

Run everything manually at any time with `pre-commit run --all-files` (add
`--hook-stage pre-push` for the push-stage hooks).

## Observability & access control

- **Audit logging**: every question logs one structured JSON record (question, user/channel,
  per-domain query specs, row count, duration, per-stage timings, and answer) via
  [app/audit/logger.py](app/audit/logger.py) — useful for debugging bad query generations and for
  auditing what data was surfaced in Slack. Goes to stdout always, plus an optional rotating file
  (`AUDIT_LOG_FILE`) so records survive a container restart instead of only existing in however
  stdout happens to be captured.
- **Per-stage timing**: each audit record includes a `timings` breakdown (`classify_ms`,
  `resolve_customer_anchor_ms`/`resolve_vendor_anchor_ms` when the cross-domain geo pattern
  applies, one `<domain>_agent_ms` per fanned-out domain, `synthesize_ms`, `graph_ms`) so a
  slow/failing question can be attributed to a specific stage instead of only showing total
  `duration_ms`. Timings are captured via a context manager whose `finally` runs *before* the
  failure is logged, so the stage that actually failed still shows its own duration (see
  `_timed_stage` in [app/rag/pipeline.py](app/rag/pipeline.py)).
- **Gemini retry/backoff**: transient errors (429/5xx **and** client-side HTTP timeouts) are
  retried with exponential backoff (`GEMINI_MAX_RETRIES`, `GEMINI_RETRY_BASE_DELAY_SECONDS`),
  capped by a hard wall-clock deadline (`GEMINI_MAX_RETRY_SECONDS`) so retries can't stack into an
  unbounded stall, in [app/llm/gemini_client.py](app/llm/gemini_client.py). Every Gemini-calling
  path (classification, each domain agent, answer synthesis) is wrapped with graceful fallback
  messages if retries are exhausted — none can crash the request unhandled.
- **Circuit breaker**: layered on top of retries — after `GEMINI_CIRCUIT_BREAKER_THRESHOLD`
  *consecutive* failed calls (retries already exhausted within each), every further call fails
  immediately for `GEMINI_CIRCUIT_BREAKER_COOLDOWN_SECONDS` instead of paying the full
  retry/timeout cost again, so a sustained outage can't tie up every Socket Mode worker thread at
  once ([app/llm/circuit_breaker.py](app/llm/circuit_breaker.py)).
- **Per-user rate limit**: `USER_RATE_LIMIT_PER_MINUTE` bounds how often one (channel, user) pair
  can ask a question ([app/rag/rate_limiter.py](app/rag/rate_limiter.py)), checked before any
  Gemini/Mongo work — independent of the shared daily budget below, so one chatty user can't
  monopolize it.
- **Daily call budget**: `GEMINI_DAILY_CALL_BUDGET` caps Gemini requests per process per day
  ([app/llm/quota.py](app/llm/quota.py)); once exceeded, the bot replies with a friendly message
  instead of calling Gemini. In-process only — resets on restart, not shared across instances.
  **This (along with the answer cache, clarification cache, and rate limiter, all likewise
  in-process) is the reason horizontal scaling (multiple bot instances) isn't a drop-in
  throughput fix today** — all four would need to move to a shared store (Mongo/Redis) first.
- **DB indexes ensured at startup**: `app.main` calls `ensure_indexes` (idempotent) against
  `orders`/`users` on the fields the agent graph filters on constantly — `customer_id`,
  `vendor_id`, `status`, `created_at`, `user_id`, `usertype`, plus the `2dsphere` geo index
  ([app/db/indexes.py](app/db/indexes.py)) — so those queries don't silently degrade into full
  collection scans as data grows.
- **Slack access control**: `SLACK_ALLOWED_CHANNEL_IDS`/`SLACK_ALLOWED_USER_IDS` restrict who can
  query the bot ([app/slack/access_control.py](app/slack/access_control.py)). Unauthorized
  mentions/DMs are silently ignored; an unauthorized `/ask` gets a visible denial.
- **Shared clients, not per-request**: a single `GeminiClient` is constructed once at startup
  ([app/main.py](app/main.py)) and injected into `register_handlers`, and `MongoClient` is a
  process-wide singleton ([app/db/mongo.py](app/db/mongo.py)) with explicit connection timeouts
  and a bounded pool — neither is recreated per question. `app.main` also logs a startup warning
  if `MONGODB_MAX_POOL_SIZE` doesn't cover worst-case concurrent fan-out (see
  `Settings.pool_size_warning`).
- **Graceful shutdown**: `SIGTERM`/`SIGINT` close the Socket Mode connection and the pooled
  `MongoClient` before the process exits ([app/main.py](app/main.py)), instead of leaving
  connections orphaned.

## Safety notes

- The database user in `MONGODB_URI` should be **read-only**; the app also blocks
  `$where`, `$function`, `$accumulator`, `$merge`, and `$out` at the application layer, and
  rejects any collection not listed in `MONGODB_ALLOWED_COLLECTIONS` (including `$lookup`
  targets).
- Every query has a result limit (`MONGODB_MAX_RESULT_LIMIT`) and a server-side timeout
  (`MONGODB_QUERY_TIMEOUT_MS`).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, coding conventions, and PR expectations.

### Scope checklist for contributors

Before opening a PR, confirm:

- [ ] **Read-only by construction** — no new code path writes to MongoDB via the query-answering
  pipeline (seeding/introspection scripts are the only intentional exceptions).
- [ ] **`validator.py` stays the sole safety gate** — any new query-shaping logic goes through
  `validate_query_spec`, not around it; no new operator is allowed without updating
  `BANNED_OPERATORS` deliberately, not by omission.
- [ ] **`MONGODB_ALLOWED_COLLECTIONS` (and `$lookup` targets) still enforced** for anything that
  touches the database.
- [ ] **No hardcoded secrets** — `gitleaks` passes locally (`pre-commit run gitleaks`).
- [ ] **New logic has tests**, fully mocked (no live Slack/Mongo/Gemini calls in `pytest` — follow
  the `_StubGemini`/`_FakeDb` pattern in `tests/test_pipeline.py`).
- [ ] **`pytest`, `ruff check .`, `ruff format --check .`, `bandit -r app`, `pip-audit` all pass**
  locally (or via `pre-commit run --all-files --hook-stage pre-push`).
- [ ] **Schema/prompt changes** are reflected in `schema_annotations.json` if they change what
  Gemini is told about the data.
- [ ] **README/CONTRIBUTING updated** if setup, env vars, or contributor workflow changed.
- [ ] **Out of scope for now** (see plan history) — don't add vector search/embeddings, a chosen
  hosting target/CD pipeline, or external error-tracking (Sentry, etc.) without discussing first;
  these were deliberately deferred.

## Status

Core pipeline, Slack handlers, and tests are implemented and verified end-to-end against live
Gemini (`gemini-3-flash-preview`) and a seeded `orders` collection — count questions, calculations
over the `payby` breakdown, and out-of-scope questions all produced correct/graceful answers.

Remaining before production use:
- Point `MONGODB_ALLOWED_COLLECTIONS` at real collections (currently seeded demo data in
  `orders`) and re-run `app.db.introspect` + review `schema_annotations.json` for your actual schema.
- Run a real end-to-end test in a Slack channel (see task checklist, task #9).
