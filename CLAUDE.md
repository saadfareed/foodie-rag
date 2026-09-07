# CLAUDE.md — project map

Read this first, before grepping the codebase. It's the whole picture in one file: what this
project is, how the pieces fit together, what invariants must not break, and where the deeper
detail lives. For exhaustive function-by-function detail and sequence diagrams, see
[docs/architecture.md](docs/architecture.md) — read that when you need to trace an exact call
path, not for a first orientation.

## What this is

A Slack bot: users `@mention` it, DM it, or run `/ask <question>`; it turns the question into a
safe, read-only MongoDB query via Google's Gemini (free tier), executes it, and replies with a
natural-language answer — plus, when asked, a **CSV, XLSX, or PDF report** attached to the reply.
Python 3.12+, `slack-bolt` (Socket Mode, no public URL needed), `google-genai`, `pymongo`,
`langgraph`/`langchain-core` for the agent orchestration, `pydantic` for every structured shape
the LLM produces, and `openpyxl`/`matplotlib`/`WeasyPrint` for the report formats.

Two core design principles:

1. **The LLM never authors a raw MongoDB query or picks its own collection.** It fills out a
   `QuerySpec` (structured data), and code — not the prompt — decides which collection it's
   allowed to touch, forces any required filter, and validates the rest before anything reaches
   the database.
2. **The LLM never sees a field it isn't allowed to answer with.** `app/security/field_policy.py`
   is applied at the single point where rows leave MongoDB, so storage plumbing (`_id`, `__v`,
   index fields) and credential values are gone before anything downstream — the answer prompt,
   an exported file, the cache, the audit log — can see them.

Read [Guardrails](#guardrails-do-not-weaken-these) below before changing anything in the request
path.

## Directory map

```
app/
  config.py        Settings singleton -- every env var this app reads, validated at import time
  agents/          Multi-domain LangGraph agent: classify -> resolve anchors -> fan out -> synthesize
    classifier.py    question -> domain(s) + confidence + clarification + context_mode/resolved_question
                     + output_format (all enum-constrained, all from ONE Gemini call)
    domains.py        domain registry; forces collection/usertype scoping in code (THE guardrail)
                      + per-domain report column order / hidden columns
    enrichment.py      customer_id/vendor_id -> customer_name/vendor_name, in code, one $in query
    query_agents.py   per-domain schema-scoped query generation
    graph.py           the StateGraph itself
    state.py            GraphState TypedDict (the graph's schema)
  db/
    mongo.py         pooled MongoClient singleton, explicit timeouts
    indexes.py         idempotent compound-index creation, called at every startup
    introspect.py      CLI: samples collections -> schema_summary.json (offline, not live path)
    executor.py         runs a *validated* QuerySpec, applies the field policy to every row out
    seed.py, seed_users.py   demo-data generators (offline, not live path)
  security/
    field_policy.py   THE field allow/deny policy: drop internal, redact secret (one source of truth)
    output_scanner.py   last-resort regex redaction over the model's finished prose
  services/
    intent_router.py  deterministic (no Gemini) explicit-format detection + policy refusals
  generators/       Report formats, all built on one shared tabular layer
    tabular.py        rows_by_domain -> bounded ReportTable (column order, row/column caps)
    charts.py          rule-based pie/bar/line selection + thread-safe Figure rendering (no pyplot)
    csv_generator.py    titled CSV blocks, stdlib csv, formula-injection guarded
    xlsx_generator.py    styled openpyxl workbook, one sheet per domain
    pdf_generator.py      fixed Jinja2/WeasyPrint template: title -> insights -> chart -> table
    render_pool.py         bounded thread pool + timeout, so rendering can't saturate all workers
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
    handlers.py       app_mention / DM / `/ask` / `/login` / `/logout`, all routing into
                      pipeline.answer_question; uploads any generated file
    access_control.py   channel/user allowlists
    auth.py              mock `/login` vendor sessions (TTL-bounded), scopes queries to one vendor
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

**Every gate that costs nothing runs before every gate that costs something.** Nothing reaches
Gemini until all of them have passed — that ordering is load-bearing, not cosmetic.

```
Slack event -> handlers.py -> access_control.is_authorized (+ auth.get_authenticated_vendor)
  -> pipeline.answer_question:
       "reset"/"new topic"/"start over"/"forget that" (exact match)?
          -> clear clarification/conversation-context/context-switch state, reply, done (no Gemini)
       pending context_switch_cache confirmation for this user?
          -> yes/no (exact match, no Gemini): yes answers the parked candidate question fresh;
             no keeps current context; anything else drops the prompt and falls through below
       intent_router.refusal_reason (regex: credentials / DB internals) -> refuse, done (no Gemini)
       intent_router.detect_explicit_format (regex: "as csv" / "excel" / "pdf")
       answer_cache (hit? return immediately, zero cost -- key includes vendor scope + format)
       -> rate_limiter (per channel+user; before any Gemini/Mongo work)
       -> quota_tracker (shared daily Gemini budget -- BEFORE any Gemini call, not after one)
       -> clarification_cache (merge with a pending follow-up if any)
       -> conversation_context_cache (only if no clarification pending: last resolved_question,
          if any, threaded in as previous_question)
       -> agent graph (app/agents/graph.py):
            classify (ALSO decides context_mode/resolved_question and output_format -- one call)
            -> if context_mode=="new_topic" AND previous_question is live: confirm_context_switch
               (asks the user before discarding it -- no query generated yet)
            -> else [resolve_anchors if needed] -> fan out to domain_agent (parallel, one per domain)
               each domain_agent: generate -> scope -> validate -> execute -> **sanitize rows**
                                  -> enrich_rows_with_names (ids -> names; no-op when no ids)
            -> synthesize
       -> clarification/context-switch handling / conversation_context_cache.set
       -> output_scanner.scan_output_for_pii over the prose
       -> if a format was requested: generators/* on the bounded render pool (failure is
          non-fatal -- the text answer still ships)
       -> audit log / answer_cache.set (text AND file bytes) -> AnswerResult
```

`answer_question` returns a single `AnswerResult` (text + optional file bytes/type), never
"a str, except sometimes a dict" — that dual return type is what previously let a cached report
replay as bare prose with the attachment silently missing.

Every Gemini call funnels through `GeminiClient._call_with_retry`:
`circuit_breaker.before_call() -> quota_tracker.record_call() -> retry loop -> the API`.

## Guardrails (do not weaken these)

- **`app/agents/domains.py::scope_spec_to_domain`** forces `collection` and (if applicable)
  `usertype` onto every generated `QuerySpec`, in code, after generation — regardless of what the
  model wrote. This is *the* reason a "vendors" question can't read customer rows. Any new domain
  or new shared-collection pattern must go through this, not around it.
- **`app/rag/validator.py::validate_query_spec`** is the sole safety gate before execution: bans
  `$where`/`$function`/`$accumulator`/`$merge`/`$out` (incl. inside `$lookup` targets), enforces
  the collection allow-list, clamps `limit`/date-range/geo-radius, and rejects any spec that
  references a secret-valued field. A new LLM-facing capability extends `QuerySpec` + this
  validator together — never lets generated content skip it.
- **`app/security/field_policy.py` is the one and only field allow/deny policy**, and it is
  applied in three layers that must stay in that order: hidden from the schema prompt
  (`schema_context.py`), rejected if the model names one anyway (`validator.py`), stripped from
  every row regardless (`executor.py`). Never add a fourth copy of these rules, and never move
  sanitization downstream of `executor.py` — sanitizing at the exit is a filter that can be
  forgotten; sanitizing at the entrance covers every path by construction, including ones added
  later. The validator checks only `is_secret_field`, not `is_denied_field`: `_id` is legitimate
  pipeline *syntax* (`$group` keys, `{"$project": {"_id": 0}}`), so rejecting it there would
  refuse most valid aggregations while protecting nothing the executor doesn't already strip.
- **The answer cache key must include the authenticated vendor scope.** A vendor-scoped answer
  contains only that vendor's rows; a key without the scope replays one vendor's data to the
  next person asking the same words in the same channel. That is a cross-tenant leak, not a
  stale-answer annoyance.
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
  `conversation_context_cache`, `context_switch_cache`, `rate_limiter`, `quota_tracker`,
  `gemini_circuit_breaker`, and the `/login` vendor sessions in `app/slack/auth.py` are all
  module-level singletons with no shared backing store. This is
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
  and `GEMINI_DAILY_CALL_BUDGET` help; they don't remove the ceiling. This is why **anything
  that can be decided without a model call must be** — output format and policy refusals are
  regex + a field on the existing classifier `Classification`, not a second Gemini call. A
  dedicated "classify the intent" call previously cost a full quota unit per question to return
  one word, *and* called the transport without `query_generation_config` so it ran with thinking
  enabled. Don't reintroduce a standalone call for a decision the classify call can carry.
- **Ids are resolved to names in code, not by a model-authored `$lookup`**
  (`app/agents/enrichment.py`). A `$lookup` would need its own `usertype` scoping to avoid
  joining a customer row onto a vendor column — exactly what `scope_spec_to_domain` exists to
  keep out of the prompt's hands. The join is identical every time, so there is nothing for a
  model to decide. The id column is dropped once its name is in hand (but only then), so a
  report never shows `USR-00031` beside `Ayesha Khan`.
- **Report presentation is declared per domain, not inferred**
  (`DomainConfig.report_columns` / `report_hidden_columns`). Mongo key order is an
  implementation detail; `report_columns` is the reading order. `report_hidden_columns` is a
  *display* list — `onlinepaymentmethod`/`isWallet`/`payby` are correct data the model can still
  answer about, just not columns a person reads. Keep it separate from
  `app/security/field_policy.py`: those fields are withheld because showing them is unsafe,
  these because showing them is unhelpful. Hidden columns are kept anyway if hiding them would
  empty the table.
- **The report title comes from the classifier** (`Classification.report_title`), on the call
  that was happening anyway, so "last 10 incomplete order details in csv" produces a document
  titled *Last 10 Incomplete Order Details* rather than the generic `REPORT_TITLE`.
- **Chart type is chosen by rule, not by the model** (`app/generators/charts.py::choose_chart`):
  a temporal dimension is a line, few positive categories are a pie, everything else is a bar.
  The dimension is a column the question named (including via `_DIMENSION_SYNONYMS` — asking for
  *incomplete* orders is asking about `status`), falling back to the *lowest-cardinality*
  non-constant column. That fallback is what stops a chart being drawn against a unique
  identifier (24 bars, one per order id, saying nothing); the synonym step is what makes an
  incomplete-orders report break down by where those orders are stuck. Asking Gemini to pick
  would add a round trip to the most quota-constrained path for a decision that follows
  mechanically from the data's shape.
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

- **A bare `card` field is a payment *method*, not a card number.** `payby: {"cash": 100}` /
  `{"card": 60}` breaks an order's amount down by method, so a secret-field pattern matching
  bare `card` turns a legitimate payment breakdown into `card=[REDACTED]`. The patterns in
  `app/security/field_policy.py` require a qualifier (`card_number`, `credit_card`,
  `cardholder`) for exactly this reason.
- **Datetimes reach the report layer as strings, not datetimes** — `app/db/executor.py`'s
  `_to_jsonable` stringifies them on the way out of Mongo. `str(datetime)` is
  `"2026-09-07 20:14:38.461897+00:00"`: microseconds nobody asked for plus an always-UTC offset,
  in the widest column of the table. `tabular.render_cell` re-parses and trims that; it handles
  both the string and the object form.
- **"order" is too generic to match a column on.** `_mentioned_in` filters
  `_GENERIC_HEADER_WORDS` before matching, because "Order Type" and "Order Payment" both contain
  it and so does nearly every question — matching on it marks every column as mentioned and
  collapses the chart dimension back to a pure cardinality tie-break.

- **`$limit` can only be pushed down past a *leading* run of `$match` stages, and only when
  every stage after it is 1:1** (`app/db/executor.py::_build_pipeline`). Two traps: putting it
  first would land it ahead of the forced `usertype` `$match` that `scope_spec_to_domain`
  prepends — taking 50 arbitrary docs from the whole shared `users` collection and only then
  filtering by domain — and putting it before a later `$match`/`$unwind`/`$group` truncates the
  input to a stage that needed all of it. The allowlist is deliberately narrow
  (`$project`/`$addFields`/`$set`/`$unset`/`$replaceRoot`/`$replaceWith`); anything unproven
  falls back to the trailing limit only.
- **Never use `matplotlib.pyplot` here.** It keeps a process-global figure registry, and this
  app renders from a thread pool — two concurrent reports will interleave `plt.subplots()`/
  `plt.close()` and corrupt each other. `app/generators/charts.py` uses the object-oriented
  `Figure` API, which owns no global state. (`Figure.savefig` works without a declared backend.)
- **`openpyxl`'s `sheet.append([])` does not create a blank row and does not advance
  `max_row`.** Deriving subsequent row positions from `max_row` after one silently jams the
  header against the title. `app/generators/xlsx_generator.py` computes row indices explicitly
  instead.
- **CSV and XLSX both need the formula-injection guard.** A stored value beginning with `=`,
  `+`, `-`, or `@` is evaluated as a formula when the file is opened in Excel; both writers
  prefix such strings with an apostrophe. A new export format needs the same guard.
- **The answer cache is looked up *before* the graph runs, so it can only key on the
  *explicitly named* format**, never the classifier's inferred one. Rebuilding the key from the
  resolved format at `set` time writes to a key nothing ever reads — the same question would
  miss the cache and re-render forever. `pipeline.answer_question` deliberately reuses the one
  `cache_key` variable for both `get` and `set`.
- **`app/rag/schema_context.py` stats its two files on every lookup, on purpose.** Throttling
  that behind a timer saves microseconds and costs the documented guarantee that re-running
  `introspect.py` is picked up without a restart (there's a test for it). The memoization worth
  having is `_rendered_cache`, which skips re-rendering the prompt text, not the `stat`.

- **`isinstance` can't distinguish a test framework's log handler from a real `StreamHandler`**
  if the former subclasses the latter (pytest's `LogCaptureHandler` does) — `app/audit/logger.py`
  checks `type(h) is logging.StreamHandler` (exact type) for its idempotency guard, not
  `isinstance`, and not "handlers list is non-empty."
- **LangGraph `Send(...)` payloads are a fresh dict, not the full graph state.** A fanned-out node
  (`domain_agent`) only sees what `_fan_out` explicitly puts in its payload — see how
  `resolved_customer_location`/`resolved_vendor_ids`/`spec_cache`/`authenticated_vendor_id` are
  threaded through deliberately in `app/agents/graph.py::_fan_out`, not implicitly inherited.
  **This has already caused one silent security bug**: `authenticated_vendor_id` was set on the
  top-level state but never added to the payload, so `_orders_id_filter`/`_vendors_id_filter`
  read it from their own node's state, found nothing, and forced no vendor scoping — every
  `/login` user saw every vendor's rows. Nothing raised; the answers were just wrong. Anything a
  fanned-out node reads from state needs a test that asserts the *effect* (see
  `tests/test_graph.py`'s RBAC tests), because a missing key here fails silently by design.
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
