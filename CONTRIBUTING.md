# Contributing

Thanks for looking at this project. It's a deliberately narrow-scoped system: it turns questions
into safe, read-only MongoDB queries via Gemini, and serves them through two adapters — a Slack
bot and an embeddable web chat widget — over one pipeline. Keeping it narrow is a feature, not an
oversight, so please read the "What's out of scope" section before proposing larger changes.

## Dev setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt   # installs runtime deps too, plus pytest/ruff/bandit/pip-audit/pre-commit
cp .env.example .env                  # fill in your own Slack/Gemini/MongoDB credentials
```

Install the git hooks so the checks that gate CI also gate your commits/pushes locally:

```bash
curl -sSL https://github.com/gitleaks/gitleaks/releases/download/v8.21.2/gitleaks_8.21.2_linux_x64.tar.gz \
  | tar -xz gitleaks
mkdir -p ~/.local/bin && mv gitleaks ~/.local/bin/
pre-commit install --install-hooks -t pre-commit -t pre-push
```

See [README.md](README.md) for the full setup (schema introspection, running the bot, etc.).

## Coding conventions

This codebase follows a few consistent patterns — match them rather than introducing new ones:

- **Pure functions where possible.** `app/rag/calculation.py` and `app/rag/validator.py`'s helper
  functions take plain data in, return plain data out, no I/O. Prefer this over stateful classes.
- **Thin orchestration, fat modules.** `app/rag/pipeline.py` just sequences calls to other
  modules and handles each failure mode explicitly (returns a user-facing string, never raises
  out to the Slack handler). New failure modes should follow the same shape.
- **Config lives in `app/config.py` only.** New env vars go through `Settings.__init__`, with a
  sensible default via `os.environ.get(...)`, not scattered `os.environ` reads elsewhere.
- **Two policies, two places, both in code.** `app/security/field_policy.py` decides which
  *fields* may leave the system for anyone; `app/security/roles.py` decides which *rows* may leave
  it for a particular person. Neither is ever a prompt instruction. A new domain, role or field
  category extends one of those two files — not a check somewhere in the request path.
- **Default deny.** A missing principal reads nothing, an unresolved scope matches nothing, an
  unknown role is a rejected token. Every one of these fails silently in the other direction: the
  query runs, the answer is fluent, and it is built from rows the asker was never entitled to see.
- **State goes in `app/state/`.** If it stores something across requests, it stores it there — so
  it works across replicas, and so `tests/conftest.py` resets it for free. Anything that must be
  atomic gets a backend primitive rather than a read-modify-write on top of `get`/`set`.
- **Type hints on everything**, minimal comments. A comment should explain a non-obvious *why*
  (see the `nosec` comments in `app/db/seed.py`/`app/llm/gemini_client.py` for the pattern), not
  restate what the code does.
- **The LLM never authors raw MongoDB queries directly into execution.** It fills out a
  `QuerySpec` (`app/rag/query_spec.py`), which then passes through `validate_query_spec` before
  `execute_query_spec` ever sees it. If you're adding a new capability for the LLM, extend
  `QuerySpec` and `validator.py` together — don't let generated content skip validation.

## Testing

```bash
pytest
```

Every test is fully mocked — no real Slack, MongoDB, or Gemini calls. Follow the existing
patterns:
- `tests/test_pipeline.py`'s `_StubGemini` and `_FakeDb` classes for pipeline-level tests.
- `tests/test_query_validator.py` for safety-rule tests (one test per rule, both the pass and
  fail case).
- `caplog`-based tests like `tests/test_audit_logger.py` for anything that logs.
- **State is reset per test** by `conftest.py`'s `_fresh_state_backend`, which installs a clean
  `InMemoryBackend` — that covers every cache, limiter and counter at once. A test that fakes time
  patches `app.state.memory.time.monotonic`, not the module under test.
- **A test calling `answer_question` or `graph.invoke` must state a principal.** There is no
  permissive default, so a test that forgets one asserts a refusal instead of silently exercising
  an unrestricted path. `tests/test_pipeline*.py` define a module-level `ADMIN`.
- **Anything touching authorization asserts the *effect*** — the filter that reached the database
  (`tests/test_graph.py`), or what reached `answer_question` (`tests/test_api.py`) — never that a
  function was called. These regressions do not raise.
- `tests/test_redis_integration.py` is the one deliberate exception to "everything is mocked", and
  it skips itself unless `REDIS_TEST_URL` is set. It exists because the cross-replica guarantees
  live in two Lua scripts, which a fake client cannot exercise.

New logic needs new tests in the same style. A PR that adds a code path with no test coverage
will likely get asked to add it.

## Before opening a PR

Run through the [scope checklist in README.md](README.md#scope-checklist-for-contributors). At minimum:

```bash
pytest
ruff check .
ruff format --check .
bandit -r app
pip-audit
```

(`pre-commit run --all-files --hook-stage pre-push` runs all of the above in one shot.)

## What's out of scope (for now)

These were deliberately deferred, not forgotten — please raise a discussion before building
against them:

- **Vector search / embeddings.** Not because they're useless here — see below for where they
  would genuinely help — but because the current deployment is self-hosted MongoDB rather than
  Atlas (so `$vectorSearch` isn't available), no collection holds unstructured text, and the
  schema is small enough that the whole thing fits in a prompt. Two things would have to be true
  before this is worth building: a schema large enough that retrieval beats "send it all", or a
  corpus of unstructured text to search.

  If it is built, one rule matters more than the rest: **embed metadata, never rows.** An
  embedding of a customer record is customer data, and a vector index has no natural equivalent of
  the forced filters in `app/security/roles.py` — pre-filtering a similarity search by role is
  materially harder than adding a `$match`, and getting it wrong means the retrieval step
  silently bypasses the row policy the rest of the system is built around. Schema descriptions,
  past questions and validated `QuerySpec`s carry no customer rows and sidestep the problem
  entirely. Note also that embedding calls go through `_call_with_retry`, so they would count
  against `GEMINI_DAILY_CALL_BUDGET` unless deliberately separated.
- **A chosen hosting target or CD pipeline.** Containerization (`Dockerfile`) is in scope; picking
  and wiring up a specific host (Fly.io, a VPS, etc.) is a separate decision requiring credentials
  the maintainer provides.
- **External error tracking (Sentry, etc.).** Structured stdout JSON logging
  (`app/audit/logger.py`) is the current approach; adding an external service is a real
  decision (new account, new secret) rather than a drop-in improvement.

## Reporting issues

Open a GitHub issue. Include the question you asked the bot (if relevant), what you expected, and
— if you can share it safely — the structured audit log line for that request (redact anything
sensitive first; audit logs can contain the full question/answer/query).
