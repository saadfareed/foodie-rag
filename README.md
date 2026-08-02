# Slack → MongoDB RAG Assistant

Ask questions in Slack, get answers computed from your MongoDB data via Google's Gemini (free tier).

## How it works

1. A user @-mentions the bot, DMs it, or runs `/ask <question>` in Slack.
2. The question, plus a schema summary of your MongoDB collections, is sent to Gemini, which
   returns a structured query (`{collection, operation, filter/pipeline, ...}`) — not raw code.
3. The query is validated (allowed collection only, no destructive/JS operators, result limit
   capped) before it ever touches the database, then run read-only against MongoDB.
4. Any math (totals, averages, etc.) is either pushed into the MongoDB aggregation pipeline or
   computed in Python over the small result set.
5. Gemini turns the result rows into a concise natural-language reply, posted back to Slack.

If a question can't be answered from the available data, Gemini is instructed to say so instead
of guessing.

## Project layout

```
app/
  config.py           # loads and validates .env settings
  db/
    mongo.py           # MongoDB client/connection
    introspect.py       # samples collections -> schema_summary.json
    executor.py          # runs a validated QuerySpec against MongoDB
  rag/
    query_spec.py        # QuerySpec / QueryError pydantic models
    validator.py         # safety checks on LLM-generated queries
    schema_context.py    # schema_summary.json (+ annotations) -> prompt text
    calculation.py       # pure total/average/min/max/count helpers
    pipeline.py           # orchestrates: question -> query -> validate -> execute -> answer
  llm/
    gemini_client.py     # Gemini query-generation and answer-generation calls
  slack/
    handlers.py           # app_mention / DM / `/ask` handlers
  main.py                # Slack Socket Mode entrypoint
tests/                   # pytest suite, fully mocked (no live Slack/Mongo/Gemini calls)
```

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
   - `MONGODB_QUERY_TIMEOUT_MS=5000`
   - `MONGODB_MAX_RESULT_LIMIT=200`
   - `AUDIT_LOG_LEVEL=INFO`
   - `GEMINI_MAX_RETRIES=3`, `GEMINI_RETRY_BASE_DELAY_SECONDS=1.0`
   - `GEMINI_DAILY_CALL_BUDGET=0` (0 = unlimited)
   - `SLACK_ALLOWED_CHANNEL_IDS`, `SLACK_ALLOWED_USER_IDS` (comma-separated; empty = open to all)

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
cross-collection checks, limit clamping), the calculation helpers, schema-context building,
schema introspection's type/example logic, and the end-to-end pipeline orchestration — all with
mocked Gemini/MongoDB, so `pytest` never makes network calls or needs real credentials.

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

- **Audit logging**: every question logs one structured JSON record to stdout (the generated
  query, row count, duration, and answer) via [app/audit/logger.py](app/audit/logger.py) —
  useful for debugging bad query generations and for auditing what data was surfaced in Slack.
- **Gemini retry/backoff**: transient errors (429/5xx) are retried with exponential backoff
  (`GEMINI_MAX_RETRIES`, `GEMINI_RETRY_BASE_DELAY_SECONDS`) in
  [app/llm/gemini_client.py](app/llm/gemini_client.py).
- **Daily call budget**: `GEMINI_DAILY_CALL_BUDGET` caps Gemini requests per process per day
  ([app/llm/quota.py](app/llm/quota.py)); once exceeded, the bot replies with a friendly message
  instead of calling Gemini. In-process only — resets on restart, not shared across instances.
- **Slack access control**: `SLACK_ALLOWED_CHANNEL_IDS`/`SLACK_ALLOWED_USER_IDS` restrict who can
  query the bot ([app/slack/access_control.py](app/slack/access_control.py)). Unauthorized
  mentions/DMs are silently ignored; an unauthorized `/ask` gets a visible denial.

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
