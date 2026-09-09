# CLAUDE.md — project map

Read this first, before grepping the codebase. It's the whole picture in one file: what this
project is, how the pieces fit together, what invariants must not break, and where the deeper
detail lives. For exhaustive function-by-function detail and sequence diagrams, see
[docs/architecture.md](docs/architecture.md) — read that when you need to trace an exact call
path, not for a first orientation.

## What this is

A natural-language assistant over MongoDB, reachable two ways: a **Slack bot** (users `@mention`
it, DM it, or run `/ask <question>`) and an **embeddable web chat widget** any web application can
drop in with one script tag. Either way it turns the question into a safe, read-only MongoDB query
via Google's Gemini (free tier), executes it, and replies with a natural-language answer — plus,
when asked, a **CSV, XLSX, or PDF report** attached to the reply.

`app/slack/` and `app/api/` are two *adapters* onto one pipeline, not two implementations. Nothing
below `app/rag/pipeline.py::answer_question` knows which one a question arrived through, and both
can run at once against one database.
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

3. **The caller never asserts its own identity or role.** Slack states who is speaking; a
   browser will send whatever it is told to. So the web adapter takes both from a short-lived
   token minted server-to-server by the host application (`app/api/tokens.py`), never from the
   request body — and the resulting `Principal` (`app/security/roles.py`) is turned into a forced
   filter, in code, on every generated query.
4. **The default is deny.** An unauthenticated caller reads nothing. This used to be the opposite:
   the single `authenticated_vendor_id` axis applied no filter when absent, so "not signed in"
   meant "sees everything" — survivable when the only entrance was an allow-listed Slack channel,
   and not survivable once a browser could reach it.

Read [Guardrails](#guardrails-do-not-weaken-these) below before changing anything in the request
path.

## Directory map

```
app/
  config.py        Settings singleton -- every env var this app reads, validated at import time
  messages.py      EVERY user-facing non-answer string: failures, no-data, refusals, notes
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
    identity.py           the ONE code path allowed to read users.email/password_hash --
                          sign-in lookups only (asserted address, or address + password)
    accounts.py             the ONE code path that WRITES to users (playground sign-up, backfill);
                            needs a writable credential, never reachable from a question
    backfill_credentials.py   CLI: give existing rows an email + password_hash (dry run by
                              default; offline, not live path)
    seed.py, seed_users.py   demo-data generators (offline, not live path)
  security/
    field_policy.py   THE field allow/deny policy: drop internal/contact, redact secret (one source)
    roles.py            THE row-level policy: admin/vendor/customer -> a forced filter per domain
    passwords.py          PBKDF2 hashing/verification for users.password_hash; equal-cost failure
    output_scanner.py     last-resort redaction over the model's prose, whole or streamed
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
    rate_limiter.py        per-identity limits: a sliding window (bursts) + a daily count (drains)
    stream.py               progress/token events (NullSink for Slack, QueueSink for SSE)
  state/              WHERE EVERY STATEFUL GUARDRAIL KEEPS ITS STATE. One seam, two backends.
    backend.py          the contract: 7 primitives, chosen so each is ATOMIC in Redis
    memory.py             in-process TTL + LRU (default; the behaviour these caches always had)
    redis_backend.py       shared across replicas; two Lua scripts (counter, sliding window)
    store.py                TtlStore + key encoding, shared by every cache that has a TTL
  llm/
    gemini_client.py  all Gemini calls: retry/backoff/timeout/thinking-budget/fallback-models/quota
    circuit_breaker.py  fails fast on a sustained Gemini outage instead of retrying forever
    quota.py            shared daily call budget
  slack/
    handlers.py       app_mention / DM / `/ask` / `/login` / `/logout`, all routing into
                      pipeline.answer_question; uploads any generated file
    access_control.py   channel/user allowlists
    auth.py              mock `/login` vendor sessions (TTL-bounded), scopes queries to one vendor
  api/                THE WEB ADAPTER: same pipeline, browser-shaped. Mirrors slack/handlers.py.
    server.py           FastAPI gateway: / (role-aware sign-in page + dev playground),
                          /v1/session, /v1/identity/lookup, /v1/me, /v1/ask,
                          /v1/ask/stream (SSE), /v1/files/{id}, /widget.js, CORS,
                          /v1/dev/{login,logout,session} (playground only, loopback only)
    tokens.py             host-app keys (server-side) vs browser session tokens (short-lived JWT)
                          vs the playground's signed sign-in cookie -- three `typ`s, one key;
                          conversation id is DERIVED from the token, never accepted from the body
    files.py               bounded TTL store holding a generated report between answer and download
    static/widget.js        the embeddable widget: shadow DOM, textContent-only, no build, no deps
    static/playground.html    the gateway's own page: sign in, then chat. Admin-only sections are
                              stripped server-side, not hidden in CSS
    main.py                  entrypoint: `python -m app.api.main`
  audit/
    logger.py         structured JSON audit logging (stdout always, optional rotating file)
  main.py             entrypoint: config validation, index bootstrap, builds shared clients, Socket Mode
tests/                pytest, fully mocked (no live Slack/Mongo/Gemini calls, ever)
examples/
  supabase-app/       runnable sample host app: email one-time-code sign-in, roles, the widget
                      embedded, zero npm dependencies (otp.mjs is liftable as-is)
docs/
  architecture.md            full component map + sequence diagrams + function reference (the deep dive)
  web-plugin.md                the web adapter: security model, env vars, endpoints, embedding guide
  scaling.md                     running more than one instance: what breaks, and STATE_BACKEND=redis
  authorization.md                 roles, row-level access, and how one-time-code sign-in fits
  onboarding-a-collection.md   how to add a new collection/domain
schema_summary.json, schema_annotations.json   git-ignored, environment-specific (see below)
```

## Request flow at a glance

**Every gate that costs nothing runs before every gate that costs something.** Nothing reaches
Gemini until all of them have passed — that ordering is load-bearing, not cosmetic.

```
Slack event -> slack/handlers.py -> access_control.is_authorized (+ auth.get_authenticated_vendor)
HTTP POST  -> api/server.py    -> verify_session_token (principal + ROLE + conversation id all
                                   come from the signed token, never the request body)
  -> both call pipeline.answer_question:
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
- **A raw exception never reaches a user.** `app/messages.py` owns every non-answer string; the
  raw text goes to the audit log (`error_detail`) and the user gets the catalogue's phrasing plus
  a reference code (`E-4F2A9C`) that ties the two together. Interpolating `{exc}` into a reply
  put pymongo tracebacks and Google's quota payload into a Slack channel — an error message is
  an output channel like any other, and it was the only one without a policy. New failure paths
  add a `Failure` member, not a new string literal; `tests/test_messages.py` asserts mechanically
  that no message names an internal.
- **"No data" is not an error.** It gets its own message naming what was searched, and no
  reference code. Telling someone "something went wrong" when the honest answer is "there are
  none" sends them hunting a bug that doesn't exist.
- **The answer cache key must include the authenticated vendor scope.** A vendor-scoped answer
  contains only that vendor's rows; a key without the scope replays one vendor's data to the
  next person asking the same words in the same channel. That is a cross-tenant leak, not a
  stale-answer annoyance.
- **The classifier only ever names a domain** (`app/agents/classifier.py::DomainName`), never a
  raw collection or field — a hallucinated domain fails Pydantic validation structurally.
- **`app/security/roles.py` is the one and only row-level access policy.** Which rows a
  principal may be answered from is decided there and merged into the query by
  `app/agents/graph.py::_domain_filter`, in code, after generation — the same relationship
  `scope_spec_to_domain` has to the collection. Three rules matter more than the rest:
  the default is **deny** (`ANONYMOUS` reads nothing, and a domain missing from a role's set is
  refused by omission); an unresolved computed scope becomes `{"$in": []}` and **never** an absent
  filter; and an authorization filter is never widened by a geo anchor. All three fail silently if
  broken — the query runs, the answer is fluent, and it is built from rows the asker was not
  entitled to — so `tests/test_graph.py` asserts the *filter that reached the database*, not that
  a function was called.
- **A per-role limit is an *override*, never a replacement.** `settings.rate_limit_override_for`
  returns None when a role isn't listed, and `RateLimiter.allow` reads None as "use whatever this
  limiter was built with". Resolving it to a number in the settings instead would make the setting
  authoritative over the limiter object, silently overriding anything configured directly. The
  flat `USER_RATE_LIMIT_PER_MINUTE=0` stays the master switch: a role entry cannot turn limiting
  back on where it was deliberately off. `REPORT_MAX_ROWS_BY_ROLE` is the same shape, and
  `build_table` clamps whatever it is given to `REPORT_MAX_ROWS` so an override can only lower the
  cap, never raise it past the bound the render pool was sized for.
- **`users.email` / `users.phone` / `users.password_hash` exist only so someone can sign in.** The field policy drops them
  from every row (`_CONTACT_FIELD_PATTERNS`), so no question can reach them however it is phrased,
  and `app/db/identity.py` is the single code path allowed to read them — exact match, fixed
  projection, active accounts only. A model that can see a contact column can be asked to list it,
  which turns an authentication field into a contact-scraping endpoint with natural-language
  search over it. `REPORT_INCLUDE_CONTACTS` (off by default) opens one narrow exception on the
  *report* path only, governed by `roles.may_see_contacts` — which is a strictly narrower question
  than `may_query`: a customer reads the vendor directory *unfiltered*, so contacts there would be
  every vendor's phone number in one download. That subset relationship has its own test.
- **The web adapter reads identity, role and conversation from the signed token only**
  (`app/api/server.py` -> `app/api/tokens.py::SessionClaims`). A browser can put `vendor_id` or a
  conversation id in the request body all day; nothing reads them. `WIDGET_ALLOWED_SESSION_ROLES`
  additionally limits which roles a host key may assert, and `admin` is not in the default: a host
  key is a long-lived secret on someone else's server, and leaking one should not confer the
  ability to mint an unrestricted session. This is the browser equivalent
  of `scope_spec_to_domain` — a guarantee by construction rather than a validation rule someone
  has to remember — and, like the `Send(...)` payload bug below, it fails *silently* if broken:
  every request still succeeds and simply answers with the wrong person's data. `tests/test_api.py`
  asserts the effect (what reaches `answer_question`), not the plumbing.
- **A `WIDGET_API_KEYS` entry is `tenant:secret` only when the prefix is a plain identifier.**
  Splitting on the first colon unconditionally silently reinterpreted a generated secret that
  happened to contain one as a tenant plus a much shorter secret — the host application sent the
  whole string, the gateway compared it against the tail, and every sign-in failed with a 401
  naming nothing. Generated keys contain punctuation often enough that this is a matter of when,
  not if.
- **The dev playground is a hole in the credential boundary, fenced three ways.**
  `WIDGET_DEV_PLAYGROUND` lets the landing page mint a session with no host-app key, so it is off
  by default, refused for any non-loopback *peer* (a forwarded header is not enough -- anyone
  could claim to be local), and still bound by `WIDGET_ALLOWED_SESSION_ROLES` plus the same
  live-account check. It shares `_check_session_request` with `/v1/session` so a playground
  session can never be more permissive than a real one: a playground that is easier to pass than
  production teaches the wrong lesson.
- **`WIDGET_API_KEYS` is a server-side credential and `WIDGET_JWT_SECRET` signs browser tokens;
  they are separate on purpose.** Rotating a host app's key must not invalidate every live chat
  session, and leaking one must not confer the ability to mint the other. A browser holding a
  host-app key could mint a session for any user at any scope, so a host application's *server*
  is the only thing that may ever call `POST /v1/session`.
- **A sign-up form names its errors; a sign-in form never does.** `app/messages.py`'s
  `invalid_credentials_message()` is deliberately identical for every way a sign-in can fail, so
  the form can't be used to test whether an address is registered. `AccountError`
  (`app/db/accounts.py`) is the opposite on purpose: on a *sign-up* the person is telling us the
  address rather than guessing it, and "that address is taken" is the one thing they need. Don't
  unify the two -- they are answering different questions.
- **The playground authenticates; it does not ask who you are.** `POST /v1/dev/login` takes an
  email and a password, and `app/db/identity.py::authenticate_password` decides both the identity
  and the role from the account (`usertype`) -- neither is ever read from the page. The previous
  form asked for a role and a `user_id`, which was the wrong question asked of the wrong person:
  nobody knows their own `USR-00031`, and a role you type is a role you chose. The cookie it sets
  is a signed JWT with its own `typ` (`app/api/tokens.py::mint_dev_login_cookie`) for the same
  reason: next to a password field, a `role:user_id` string the page writes itself would be a lie
  anyone could edit to say `admin`. Every failed sign-in -- unknown address, wrong password,
  suspended account, role the gateway won't mint -- is **one message and one cost**
  (`verify_password` hashes even when there is no account), because any difference is a way to
  test whether an address is registered.
- **`users.password_hash` is stored, never seen.** `app/security/passwords.py` owns the format
  (PBKDF2-HMAC-SHA256, per-record salt, cost factor carried in the record so it can be raised
  without invalidating anyone). The field policy already matches it as a secret field, so the
  value is replaced on the way out of `app/db/executor.py` and no question can reach one; the
  domains hide the column from reports as well, because a column of `[REDACTED]` is unhelpful
  rather than unsafe. A new credential column needs nothing new -- it needs to match the existing
  patterns.
- **The widget's opening line comes from `GET /v1/me`, not from the widget.** What a person may
  ask about is `app/security/roles.py`'s answer, and a greeting written in JavaScript is a second
  copy of it in the one place that cannot see it -- so a customer would be invited to "ask about
  your orders, customers or vendors" and then refused for two thirds of it. `app/messages.py`
  owns the wording, like every other non-answer string; `data-greeting` on the script tag is the
  host's override and skips the request entirely.
- **The landing page's admin sections are stripped server-side.** `app/api/server.py::landing`
  removes the endpoint reference and the embedding guide from the HTML for anyone whose signed
  cookie doesn't say `admin`. Hiding them with CSS would ship them to everyone and call it a
  preference; this is the same "by construction, not by remembering" shape as
  `scope_spec_to_domain`, applied to a page.
- **`app/api/static/widget.js` writes every server-provided string with `textContent`.** Slack
  renders text; a browser renders markup. Answers are generated from database rows, so an
  `innerHTML` path here turns one poisoned row into stored XSS on every customer site embedding
  the widget. There is deliberately no HTML-rendering code in that file at all — the safety comes
  from having nothing to forget to escape, and `tests/test_api.py` asserts `textContent` is still
  in the served script.
- **Streamed tokens go through the redactor, never around it.**
  `app/security/output_scanner.py::scan_output_for_pii` runs on *finished* prose -- for a stream
  that is long after a card number has been displayed -- and re-scanning each chunk alone catches
  nothing, because a number split across two chunks matches neither half. `StreamingRedactor`
  releases text only up to a character no pattern can match. **That set (`_UNSAFE_IN_A_MATCH`) is
  derived from the patterns directly above it: a new pattern must use only those characters, or
  widen the set.** A pattern needing letters would make almost nothing a safe cut, at which point
  the strategy needs rethinking rather than patching. `tests/test_output_scanner.py` asserts the
  property that matters -- streaming *any* chunking of a text equals scanning the whole text.
- **Anything shared across replicas must be atomic in the backend, not assembled in Python.**
  `app/state/backend.py` deliberately exposes `incr` and `allow_in_window` as primitives rather
  than a general key-value map. Read-modify-write over `get`/`set` is a race: two replicas both
  read 19, both write 20, and 21 calls have been spent against a budget of 20 -- while the budget
  still reports itself as enforced. A new shared guardrail extends that protocol; it does not
  build its own read-then-write on top of the existing primitives.
- **`MONGODB_URI` must be a read-only credential.** The app-layer checks above are a second
  layer, not a substitute. `app/db/accounts.py` is the single exception and is scoped to be one:
  it is the only module in `app/` that writes, its two callers are the dev playground's sign-up
  form and an offline CLI, and against a read-only credential it fails loudly with a message
  saying so rather than silently degrading. Nothing on the question path may import it.
- **Config lives only in `app/config.py`.** New env vars go through `Settings.__init__` via the
  `_require`/`_int`/`_float`/`_parse_list` helpers (which raise naming the offending variable),
  not scattered `os.environ` reads elsewhere.
- **Shared clients are singletons, not per-request.** One `GeminiClient` (built in `app.main`,
  injected everywhere) and one pooled `MongoClient` (`app/db/mongo.py::get_client`). Don't
  construct either inside a request path.

## Known limitations / tradeoffs (already decided, don't re-litigate without new information)

- **Every stateful guardrail is in-process only**: `answer_cache`, `clarification_cache`,
  `conversation_context_cache`, `context_switch_cache`, `rate_limiter`, `quota_tracker`,
  `gemini_circuit_breaker`, the `/login` vendor sessions in `app/slack/auth.py`, and the web
  adapter's report `file_store` (`app/api/files.py`) are all
  module-level singletons with no shared backing store. This is
  *the* reason running more than one bot instance isn't a drop-in throughput fix today — each
  replica would enforce its own daily budget, cache, rate limit, and follow-up context
  independently. Revisit with Mongo/Redis-backed state first if that's ever needed. The web
  adapter adds one more reason: a download would have to land on the replica that generated the
  file, since that's the only one holding the bytes.
- **The web adapter does not stream.** `answer_question` returns a complete `AnswerResult`, so the
  widget shows a typing indicator rather than tokens appearing. Streaming means an SSE endpoint
  *and* a pipeline that yields — a real change through every layer, not a flag.
- **The gateway authenticates nobody in production.** It takes a verified identity and a role as inputs; the
  host application proves who someone is. That is why `/v1/identity/lookup` exists (the host app
  needs to resolve an address without its own Mongo credential) and why the one-time-code flow
  lives in `examples/supabase-app/otp.mjs` rather than in `app/`. Moving authentication into the
  gateway would make it an identity provider, which is a different product with different
  obligations. The one exception is deliberate and fenced: the dev playground's own sign-in form
  (`/v1/dev/login`), which is off by default, loopback-only, and exists so a developer can see the
  thing work before writing a host application.
- **Contact details are invisible in *answers*, and available in *reports* only when switched
  on.** The model is never shown them; `REPORT_INCLUDE_CONTACTS` adds the columns in code, after
  the answer, to already-authorized rows. Making the bot *answer* contact questions needs a
  role-aware field policy — the principal threaded into `app/db/executor.py` — and the field
  policy is deliberately applied at a choke point where no role is in scope. The trade is stated
  in `_CONTACT_FIELD_PATTERNS`; make it deliberately rather than by loosening the choke point.
- **Revocation is bounded by the session TTL, not immediate.** JWTs are stateless, so disabling an
  account stops new sign-ins (`WIDGET_VERIFY_ASSERTED_IDENTITY` re-checks at every renewal) and
  leaves an issued token valid for up to `WIDGET_SESSION_TTL_SECONDS` — 15 minutes by default,
  which *is* the mechanism rather than a workaround. A `revoked_before:{tenant}:{sub}` timestamp
  in `app/state`, checked in `verify_session_token`, is the fix if that has to be instant.
- **One conversation per web principal.** `SessionClaims.conversation_id` is `web:{tenant}:{sub}`,
  mirroring a Slack DM; `reset` starts a new topic. Named threads would need a thread claim minted
  server-side, and must stay server-side for the reason in the guardrails above.
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
- **Ids are resolved to the person in code, not by a model-authored `$lookup`**
  (`app/agents/enrichment.py`). A `$lookup` would need its own `usertype` scoping to avoid
  joining a customer row onto a vendor column — exactly what `scope_spec_to_domain` exists to
  keep out of the prompt's hands. The join is identical every time, so there is nothing for a
  model to decide. The id column is dropped once its name is in hand (but only then), so a
  report never shows `USR-00031` beside `Ayesha Khan`.
  The same join answers **"orders with customer details"**, which used to be refused by both
  domain agents at once -- orders because user fields aren't in its schema, customers because
  order status isn't in theirs. The party's own columns (city, loyalty tier, category, rating)
  ride along on the same `$in`, but only when the question asks about the *people*
  (`wants_related_details` -- "customer details" yes, "order details" no), because attaching five
  columns to every order export is a cost paid by every question to serve a few.
  `DomainConfig.enriched_columns` is the single declaration the agent's prompt, the report column
  order and enrichment all read, so none of them can believe in a different set. An attribute
  constant on every row is dropped: a vendor's own orders all carry the same vendor, and those
  columns spend table width restating the filter -- on a real PDF they pushed `created_at` past
  `REPORT_MAX_COLUMNS` and off the page.
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
- **A chart's *measure* is a count or a sum depending on the question, and getting it wrong is
  invisible.** `choose_chart` used the first numeric column unconditionally; on an orders table
  that is `amount`, so "how many orders are incomplete" drew a pie of *money* per status -- three
  orders, one in each status, rendered 47.2% / 42.5% / 10.3% when every correct answer was 33.3%.
  `ChartSpec.value_column is None` now means "count rows", chosen when the question asks how many
  (`asks_for_a_count`). Nothing about a wrong measure looks wrong on the page, which is what makes
  it worth a rule rather than a glance: the percentages are internally consistent, the labels are
  right, and only the arithmetic against a question nobody re-reads is off.
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
  The *other* cross-domain shape — "orders together with the customer's or vendor's own details"
  — is deliberately **not** an anchor pattern and never fans out: it is one join, always the same,
  so it belongs to `app/agents/enrichment.py`, and the classifier is told to keep such questions
  single-domain. A question needing a second *query* is a planner problem; a question needing a
  second *column* is not.
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
- **`tests/conftest.py` refuses real `MongoClient` construction** (`_no_real_database`, autouse).
  A test that reaches a live database passes or fails according to what happens to be seeded on
  the machine running it — two rate-limit tests had quietly come to depend on that, staying green
  locally and failing only in CI. Patch `app.agents.graph.get_db` with a fake; the one module
  whose subject *is* the client opts out with `@pytest.mark.uses_mongo_client`.
- **CI has no `.env`**, and `app/config.py` `_require()`s six variables while building its
  Settings singleton *at import time* — so `import app.anything` fails without them. The Tests
  job in `.github/workflows/ci.yml` supplies obvious placeholders; they only need to be non-empty,
  and must never become real credentials.
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
- `tests/test_api.py` covers the web adapter, and is organised around the one thing a browser
  adds that Slack didn't: a client that will send whatever it is told to. The tests that matter
  most there assert what *reaches* `answer_question` (scope, principal, conversation id) rather
  than what the endpoint returns — a regression in any of those fails silently, answering happily
  with the wrong person's rows. `answer_question` is patched to record its arguments; a
  `@pytest.mark.answer_result(...)` marker sets what it returns, so the fake stays a fake rather
  than growing a switch on the question text.
- `tests/test_negative.py` is the adversarial suite, organised by *where the bad input comes
  from* (question / model output / database rows / infrastructure / boundaries), because that's
  what determines which guardrail should catch it. Its `assert_clean()` helper checks every reply
  against a list of internals that have actually leaked before — add to that list rather than
  writing a one-off assertion. New failure paths belong here as well as in their unit test: this
  is the suite that proves the bot explains rather than crashes.

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

- **An empty fan-out must still reach `synthesize`.** A domain refused *after* routing (a vendor
  whose customer scope came back over the cap) leaves `_fan_out` with nothing to send, and an
  empty `Send` list ends the graph with no `answer` key at all. It returns the string
  `"synthesize"` in that case, which renders whatever is in `out_of_scope_by_domain`.
- **`merge_forced_filter` combines clauses under `$and`, it does not merge dicts.** Two
  restrictions on the same field must both hold — a vendor's own `vendor_id` and a geo anchor's
  `$in` are not interchangeable — so a test asserting on a forced filter has to flatten the `$and`
  rather than reading the top-level key (see `_flat` in `tests/test_graph.py`).
- **In `app/state/memory.py`, `delete()` must clear all three maps.** Values, counters and
  windows are separate dicts but one namespace to the caller. A `delete` that reached only the
  value map is exactly why `CircuitBreaker.record_success()` silently failed to reset its own
  failure count -- the breaker opened and never closed, and nothing raised.
- **A non-positive TTL means "never expires", not "expire immediately".** That is the convention
  the rest of the config uses for a zero setting, and `VENDOR_SESSION_TTL_SECONDS=0` depends on it
  (there's a test). `app/api/files.py` is the deliberate exception: it clamps a non-positive TTL to
  a short one, because "keep every generated report forever" is the wrong reading of a typo in the
  one store that holds customers' query results.
- **`GEMINI_MAX_RETRY_SECONDS` must exceed `GEMINI_REQUEST_TIMEOUT_MS`, or nothing slow is ever
  retried.** The budget is measured from the start of the *first attempt* and covers the attempts
  as well as the backoff, so a call that fails slowly has already spent it before the first retry
  is considered — and the failures worth retrying (a 504, the client timeout itself) are slow by
  construction. Shipped as 8s vs 15s, which meant `_is_retryable`'s explicit handling of
  `httpx.TimeoutException` never once fired in production; a real `504 DEADLINE_EXCEEDED` at
  12.1s was refused a retry and a configured fallback model went unused. `settings.
  retry_budget_warning()` now says so at startup. Note the corollary when reading either number:
  the budget bounds when an attempt may *start*, never how long it then runs, so a sweep's true
  worst case is the budget plus one attempt per model.
- **`GEMINI_REQUEST_TIMEOUT_MS` has a floor of 10000 imposed by the API, not by us.** Below it
  Gemini rejects *every* call instantly with `400 INVALID_ARGUMENT: Manually set deadline 8s is
  too short. Minimum allowed deadline is 10s.` -- so a smaller number does not tighten latency,
  it stops the bot answering anything. An 8s default shipped and did exactly that. `app/config.py`
  now clamps it up and `settings.gemini_timeout_warning()` says so at startup. The wider lesson:
  a fully-mocked test suite cannot tell you an upstream refuses your configuration -- one real
  call can, and is worth making before shipping a value the API has an opinion about.
- **A 4xx from Gemini is not an outage and must not be described as one.** `classify_exception`
  maps a non-429 4xx to `UNKNOWN` (reference code, "pass this to whoever runs the bot") rather
  than `UPSTREAM_UNAVAILABLE` ("it usually recovers on its own within a few minutes"). A 400 is a
  request this application got wrong; it will never recover, and telling users to wait sends the
  one person who could fix it away from the problem.
- **A retry test whose failures are instantaneous cannot see a budget being spent.** Every test in
  `tests/test_gemini_retry.py` mocked `time.sleep` and raised immediately, under a 60s ceiling —
  so they proved the retry *predicate* and were blind to the interaction that actually broke.
  The ones that matter now drive a fake clock where a failure costs the request timeout.
- **A per-model circuit breaker needs a per-model `name`.** `GeminiClient._breaker_for` keeps one
  breaker per model because a shared one meant a model exhausting its daily quota tripped the
  breaker for the healthy fallback models too. Now that breaker state is shared storage, the name
  is what keeps them apart -- without it a shared backend silently re-merges them and the fallback
  chain stops working again.
- **`QueueSink.finish()` makes room by dropping progress; `token()`/`stage()` drop themselves.**
  Blocking on a queue nobody is draining strands the worker thread answering the question -- a
  leak that only appears with flaky clients. Progress is a courtesy; the answer is the contract.
- **Gateway endpoints are `def`, not `async def`, and that is not an oversight.**
  `answer_question` blocks (Gemini calls, Mongo round-trips, WeasyPrint). Starlette runs sync
  endpoints in a worker thread; as `async def` the same code would hold the event loop for the
  whole question and serialise every concurrent user behind it.
- **Session tokens and file-download tokens are signed with the same key, so they carry a `typ`
  claim.** Without it a download URL — which lands in browser history, the address bar and
  referrers — would be accepted as proof of identity at `/v1/ask`. One claim closes a
  confused-deputy bug that is otherwise invisible.
- **A CSS class rule beats the user agent's `[hidden] { display: none }`.** `examples/supabase-app`
  toggles sections with the `hidden` attribute, and `.stack { display: grid }` silently overrode
  it — the signed-out page rendered the sign-in form *and* the signed-in chrome at once. The fix
  is an explicit `[hidden] { display: none !important; }`; the lesson is that hiding by attribute
  only works if the stylesheet says so.
- **`isinstance` can't distinguish a test framework's log handler from a real `StreamHandler`**
  if the former subclasses the latter (pytest's `LogCaptureHandler` does) — `app/audit/logger.py`
  checks `type(h) is logging.StreamHandler` (exact type) for its idempotency guard, not
  `isinstance`, and not "handlers list is non-empty."
- **LangGraph `Send(...)` payloads are a fresh dict, not the full graph state.** A fanned-out node
  (`domain_agent`) only sees what `_fan_out` explicitly puts in its payload — see how
  `resolved_customer_location`/`resolved_vendor_ids`/`spec_cache`/`principal`/
  `authorized_customer_ids`/`stream_sink` are threaded through deliberately in
  `app/agents/graph.py::_fan_out`, not implicitly inherited.
  **This has already caused one silent security bug**: the authenticated scope was set on the
  top-level state but never added to the payload, so the id filters read it from their own node's
  state, found nothing, and forced no scoping — every `/login` user saw every vendor's rows.
  Nothing raised; the answers were just wrong. `_principal(state)` now defaults a missing
  principal to `ANONYMOUS`, which reads nothing, so the same omission fails closed rather than
  open. Anything a fanned-out node reads from state still needs a test that asserts the *effect*
  (see `tests/test_graph.py`'s RBAC tests), because a missing key here fails silently by design.
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
