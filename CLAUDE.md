# CLAUDE.md — project map

Read this first, before grepping the codebase. It's the whole picture in one file: what this
project is, how the pieces fit together, what invariants must not break, and where the deeper
detail lives. For exhaustive function-by-function detail and sequence diagrams, see
[docs/architecture.md](docs/architecture.md) — read that when you need to trace an exact call
path, not for a first orientation.

## What this is

A Slack bot: users `@mention` it, DM it, or run `/ask <question>`; it turns the question into a
safe, read-only MongoDB query via Google's Gemini (free tier), executes it, and replies with a
natural-language answer. Python 3.12+, `slack-bolt` (Socket Mode, no public URL needed),
`google-genai`, `pymongo`, `langgraph`/`langchain-core` for the agent orchestration, `pydantic`
for every structured shape the LLM produces.

The core design principle: **the LLM never authors a raw MongoDB query or picks its own
collection.** It fills out a `QuerySpec` (structured data), and code — not the prompt — decides
which collection it's allowed to touch, forces any required filter, and validates the rest before
anything reaches the database. Read [Guardrails](#guardrails-do-not-weaken-these) below before
changing anything in the request path.

## Directory map

```
app/
  config.py        Settings singleton -- every env var this app reads, validated at import time
  agents/          Multi-domain LangGraph agent: classify -> resolve anchors -> fan out -> synthesize
    classifier.py    question -> domain(s) + confidence + clarification + context_mode/resolved_question (enum-constrained)
    domains.py        domain registry; forces collection/usertype scoping in code (THE guardrail)
    query_agents.py   per-domain schema-scoped query generation
    graph.py           the StateGraph itself
    state.py            GraphState TypedDict (the graph's schema)
  db/
    mongo.py         pooled MongoClient singleton, explicit timeouts
    indexes.py         idempotent index creation, called at every startup
    introspect.py      CLI: samples collections -> schema_summary.json (offline, not live path)
    executor.py         runs a *validated* QuerySpec against MongoDB
    seed.py, seed_users.py   demo-data generators (offline, not live path)
  rag/
    query_spec.py     QuerySpec / QueryError / GeoNear pydantic models
    validator.py        the safety gate -- banned operators, collection allow-list, limit/date/geo clamps
    schema_context.py   schema_summary.json (+annotations) -> prompt text, mtime-cached
    calculation.py      pure math helpers -- NOT currently wired into the live pipeline
    pipeline.py          orchestrator: cache -> rate limit -> budget -> clarification -> graph -> audit
    answer_cache.py       per-channel TTL+LRU cache of full answers
    clarification_cache.py  per-(channel,user) "we asked a follow-up" state
    conversation_context.py per-(channel,user) last resolved_question, for short elliptical follow-ups
    context_switch_cache.py per-(channel,user) "should I clear that context?" pending confirmation
    rate_limiter.py        per-(channel,user) sliding-window rate limit
  llm/
    gemini_client.py  all Gemini calls: retry/backoff/timeout/thinking-budget/fallback-models/quota
    circuit_breaker.py  fails fast on a sustained Gemini outage instead of retrying forever
    quota.py            shared daily call budget
  slack/
    handlers.py       app_mention / DM / `/ask` handlers, all routing into pipeline.answer_question
    access_control.py   channel/user allowlists
  audit/
    logger.py         structured JSON audit logging (stdout always, optional rotating file)
  main.py             entrypoint: config validation, index bootstrap, builds shared clients, Socket Mode
tests/                pytest, fully mocked (no live Slack/Mongo/Gemini calls, ever)
docs/
  architecture.md            full component map + sequence diagrams + function reference (the deep dive)
  onboarding-a-collection.md   how to add a new collection/domain
schema_summary.json, schema_annotations.json   git-ignored, environment-specific (see below)
```

## Request flow at a glance

```
Slack event -> handlers.py -> access_control.is_authorized
  -> pipeline.answer_question:
       "reset"/"new topic"/"start over"/"forget that" (exact match)?
          -> clear clarification/conversation-context/context-switch state, reply, done (no Gemini)
       pending context_switch_cache confirmation for this user?
          -> yes/no (exact match, no Gemini): yes answers the parked candidate question fresh;
             no keeps current context; anything else drops the prompt and falls through below
       answer_cache (hit? return immediately, zero cost)
       -> rate_limiter (per channel+user; before any Gemini/Mongo work)
       -> quota_tracker (shared daily Gemini budget)
       -> clarification_cache (merge with a pending follow-up if any)
       -> conversation_context_cache (only if no clarification pending: last resolved_question,
          if any, threaded in as previous_question)
       -> agent graph (app/agents/graph.py):
            classify (also decides context_mode/resolved_question from previous_question)
            -> if context_mode=="new_topic" AND previous_question is live: confirm_context_switch
               (asks the user before discarding it -- no query generated yet)
            -> else [resolve_anchors if needed] -> fan out to domain_agent (parallel, one per domain)
            -> synthesize
       -> clarification/context-switch handling / conversation_context_cache.set / audit log /
          answer_cache.set
```

Every Gemini call funnels through `GeminiClient._call_with_retry`:
`circuit_breaker.before_call() -> quota_tracker.record_call() -> retry loop -> the API`.

## Guardrails (do not weaken these)

- **`app/agents/domains.py::scope_spec_to_domain`** forces `collection` and (if applicable)
  `usertype` onto every generated `QuerySpec`, in code, after generation — regardless of what the
  model wrote. This is *the* reason a "vendors" question can't read customer rows. Any new domain
  or new shared-collection pattern must go through this, not around it.
- **`app/rag/validator.py::validate_query_spec`** is the sole safety gate before execution: bans
  `$where`/`$function`/`$accumulator`/`$merge`/`$out` (incl. inside `$lookup` targets), enforces
  the collection allow-list, clamps `limit`/date-range/geo-radius. A new LLM-facing capability
  extends `QuerySpec` + this validator together — never lets generated content skip it.
- **The classifier only ever names a domain** (`app/agents/classifier.py::DomainName`), never a
  raw collection or field — a hallucinated domain fails Pydantic validation structurally.
- **`MONGODB_URI` must be a read-only credential.** The app-layer checks above are a second
  layer, not a substitute.
- **Config lives only in `app/config.py`.** New env vars go through `Settings.__init__` via the
  `_require`/`_int`/`_float`/`_parse_list` helpers (which raise naming the offending variable),
  not scattered `os.environ` reads elsewhere.
- **Shared clients are singletons, not per-request.** One `GeminiClient` (built in `app.main`,
  injected everywhere) and one pooled `MongoClient` (`app/db/mongo.py::get_client`). Don't
  construct either inside a request path.

## Known limitations / tradeoffs (already decided, don't re-litigate without new information)

- **Every stateful guardrail is in-process only**: `answer_cache`, `clarification_cache`,
  `conversation_context_cache`, `context_switch_cache`, `rate_limiter`, `quota_tracker`, and
  `gemini_circuit_breaker` are all module-level singletons with no shared backing store. This is
  *the* reason running more than one bot instance isn't a drop-in throughput fix today — each
  replica would enforce its own daily budget, cache, rate limit, and follow-up context
  independently. Revisit with Mongo/Redis-backed state first if that's ever needed.
- **Conversational follow-up context (`app/rag/conversation_context.py`) is deliberately
  same-session, single-turn only** — it remembers one prior *resolved* question per
  (channel, user), not a growing transcript, and expires after
  `CONVERSATION_CONTEXT_TTL_SECONDS` (default 300s). Recalling things from days/weeks ago (a
  different feature — semantic search over long-term history) was deliberately scoped out: that's
  a job for search/retrieval over accumulated Q&A, not this recency-based cache, and mixing the
  two would risk serving a stale cached *answer* instead of always re-querying MongoDB for current
  data.
- **An unrelated-looking question is confirmed, not silently answered, while prior context is
  still live.** When `previous_question` is set and the classifier decides the new message is a
  `"new_topic"`, `app/agents/graph.py` routes to `confirm_context_switch` instead of generating a
  query — the user gets "should I clear that context and answer this as a new question?" and has
  to reply yes/no before anything is queried. This is a deliberate product choice (asked for
  explicitly), not an oversight: it costs one extra round-trip on every topic switch made within
  `CONVERSATION_CONTEXT_TTL_SECONDS` of the last question, in exchange for never guessing wrong
  about whether to discard context. Once that TTL lapses (`previous_question` unset), a fresh
  question is answered immediately as before — the confirmation only fires while there's something
  live to actually discard. A user can also skip straight to a clean slate at any time by typing
  `reset` (or `new topic` / `start over` / `forget that`) — see `app/rag/pipeline.py::_is_reset_command`.
- **Google's free-tier Gemini quota is the real system-wide throughput ceiling** ("as low as
  20/day for some models" per Google), not anything in this codebase. `GEMINI_FALLBACK_MODELS`
  and `GEMINI_DAILY_CALL_BUDGET` help; they don't remove the ceiling.
- **`app/rag/calculation.py` is unused** by the live pipeline (fully tested, but not wired in).
  Math currently happens via Gemini-authored aggregation stages or Gemini reasoning over raw
  rows. Wiring it in is a real design decision (where in the graph would it run?), not a small
  patch.
- **The cross-domain anchor pattern is hand-coded, not a generic planner.** `resolve_anchors` in
  `app/agents/graph.py` only handles "vendors near a customer[, with pending orders]" explicitly.
  Adding a second cross-domain pattern means extending that function, not writing a general
  dependency graph — a generic planner was deliberately deferred as speculative machinery.
- **Vector search, a chosen hosting target/CD pipeline, and external error tracking are
  out of scope for now** (see `CONTRIBUTING.md`) — deliberately deferred, not forgotten.
- **`schema_summary.json`/`schema_annotations.json` are git-ignored and environment-specific.**
  They don't exist until someone runs `python -m app.db.introspect` against a real database; the
  LLM's understanding of the data is entirely bounded by what's in these two files.

## Testing conventions

`pytest` (config in `pyproject.toml`, `testpaths = ["tests"]`). Everything is mocked — no test
ever makes a real Slack/Mongo/Gemini call. Patterns to follow (don't introduce new ones):
- `_StubGemini` / `_FakeDb` / `_FakeCollection` / `_FakeCursor` classes, redefined per test file
  (see `tests/test_pipeline.py`, `tests/test_graph.py`) — minimal fakes exposing only the methods
  exercised, not a shared mocking framework.
- `tests/conftest.py` has **autouse** fixtures that reset every module-level singleton
  (`answer_cache`, `quota_tracker`, `rate_limiter`, `gemini_circuit_breaker`) to a disabled/clean
  state before each test — these singletons persist across the whole test run otherwise, and
  order-dependent pollution is exactly the failure mode they prevent. If you add a new
  module-level singleton with shared mutable state, add a matching reset fixture.
- `monkeypatch.setattr("app.module.settings.some_attr", value)` to override config per-test
  (settings is constructed once at import time from `.env`, so tests patch attributes on the
  already-built singleton, not environment variables — except `tests/test_config.py`, which tests
  `Settings` construction itself and does use `monkeypatch.setenv`).
- `caplog.at_level(logging.INFO, logger="audit")` for anything that audit-logs.
- One test per validator rule, both the pass and fail case (`tests/test_query_validator.py`).

Run the suite:
```bash
pytest
```
Lint (must pass before any PR, also pre-commit/CI gated): `ruff check .`, `ruff format --check .`,
`bandit -r app`, `pip-audit`.

## Common gotchas learned the hard way

- **`isinstance` can't distinguish a test framework's log handler from a real `StreamHandler`**
  if the former subclasses the latter (pytest's `LogCaptureHandler` does) — `app/audit/logger.py`
  checks `type(h) is logging.StreamHandler` (exact type) for its idempotency guard, not
  `isinstance`, and not "handlers list is non-empty."
- **LangGraph `Send(...)` payloads are a fresh dict, not the full graph state.** A fanned-out node
  (`domain_agent`) only sees what `_fan_out` explicitly puts in its payload — see how
  `resolved_customer_location`/`resolved_vendor_ids`/`spec_cache` are threaded through
  deliberately in `app/agents/graph.py::_fan_out`, not implicitly inherited.
- **A `QuerySpec` fetched from `spec_cache` must be deep-copied before mutation** — the same
  cached object is reused across call sites (anchor resolution and the real fan-out) that apply
  different `limit`/geo/id-filter overrides; mutating the shared instance would leak one site's
  overrides into the other's.
- **A model emitting `"limit": null` is not the same as omitting the field** — Pydantic only
  applies a default when the key is absent. See `QuerySpec._coerce_explicit_nulls_to_defaults`.
- **A raw generation-time exception is not a `QueryError`.** The former is a model/schema bug
  (`errors_by_domain`, "I ran into a problem"); the latter is the model's own deliberate rejection
  (`out_of_scope_by_domain`, its message shown verbatim). Don't conflate them when adding new
  failure handling.
- **`state["question"]` is not what most graph nodes should generate/answer against —
  `resolved_question` is.** `_classify_node` may rewrite a short follow-up ("what about the total
  amount?") into a self-contained `resolved_question`; `_resolve_anchors_node`, `_fan_out`, and
  `_synthesize_node` all read it via `_effective_question(state)`, never `state["question"]`
  directly. A new node added to the graph must do the same, or it'll silently generate against the
  raw, potentially incomplete fragment instead of the context-folded question.
- **`clarification_cache` and `conversation_context_cache` are mutually exclusive per request, by
  design.** `pipeline.answer_question` only reads `conversation_context_cache` when no
  clarification is pending — a pending clarification already carries context forward its own way
  (string-merging the original question with the follow-up). Consulting both would double up
  context for no benefit.
- **The reset command and the yes/no context-switch reply are matched by exact string, not
  substring or LLM judgment** (`app/rag/pipeline.py::_normalize_command` + `_RESET_PHRASES`/
  `_AFFIRMATIVE_PHRASES`/`_NEGATIVE_PHRASES`). This is deliberate: a real question that happens to
  contain "reset" or "yes" (e.g. "did we reset the counter?") must still reach the classifier, not
  get swallowed by a keyword match. Adding a new phrase to any of these sets is safe; switching the
  match to substring/fuzzy is not.
- **An ambiguous reply to the context-switch prompt drops the *old* context too, not just the
  pending prompt.** If the user replies with neither yes nor no, `pipeline.answer_question` clears
  both `context_switch_cache` and `conversation_context_cache` for that (channel, user) before
  falling through to answer the new message fresh. Keeping the old context there instead would let
  it silently re-trigger a second confirmation chained off context the user never actually
  confirmed keeping.

## Where to look for more

- [README.md](README.md) — setup, running locally/in a container, CI/CD, pre-commit hooks, full
  env var reference, contributor scope checklist.
- [docs/architecture.md](docs/architecture.md) — component map, the agent graph node-by-node, both
  entry-point sequence diagrams, a function reference for every module, and historical production
  incident notes explaining *why* several non-obvious pieces of `gemini_client.py`/`pipeline.py`
  exist.
- [docs/onboarding-a-collection.md](docs/onboarding-a-collection.md) — adding a new collection
  (and the domain registration it now requires).
- [CONTRIBUTING.md](CONTRIBUTING.md) — coding conventions, testing expectations, what's explicitly
  out of scope for now.
