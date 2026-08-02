# Architecture & request flow

This traces exactly what happens for a real question end to end: which function calls which, in
order, for both entry points (`/ask` slash command and `@mention`/DM). Generated from the actual
import graph in `app/`, not from memory.

## Component map

```mermaid
graph TD
    Slack[Slack: /ask, @mention, DM] --> Handlers[app/slack/handlers.py]
    Handlers --> AccessControl[app/slack/access_control.py]
    Handlers --> Pipeline[app/rag/pipeline.py<br/>answer_question]

    Pipeline --> Quota[app/llm/quota.py<br/>quota_tracker]
    Pipeline --> SchemaCtx[app/rag/schema_context.py<br/>build_schema_context]
    Pipeline --> Gemini[app/llm/gemini_client.py<br/>GeminiClient]
    Pipeline --> Validator[app/rag/validator.py<br/>validate_query_spec]
    Pipeline --> Executor[app/db/executor.py<br/>execute_query_spec]
    Pipeline --> Audit[app/audit/logger.py<br/>log_query_event]

    SchemaCtx --> SummaryFile[(schema_summary.json)]
    SchemaCtx --> AnnotationsFile[(schema_annotations.json)]

    Gemini --> QuotaRecord[quota_tracker.record_call]
    Gemini --> GeminiAPI[(Google Gemini API)]

    Validator --> QuerySpecModel[app/rag/query_spec.py<br/>QuerySpec / QueryError]

    Executor --> Mongo[app/db/mongo.py<br/>get_db]
    Mongo --> MongoDB[(MongoDB)]

    Audit --> Stdout[(stdout, JSON lines)]

    subgraph "Offline / CLI only - not in the live request path"
        Introspect[app/db/introspect.py]
        Seed[app/db/seed.py]
        Calc[app/rag/calculation.py<br/>unused by pipeline today]
    end
```

**Note on `app/rag/calculation.py`**: it exists (pure `total`/`average`/`minimum`/`maximum`/`count`
helpers, fully unit tested) but **nothing in the live pipeline calls it**. Math currently happens
one of two ways: Gemini writes aggregation stages (`$sum`, `$avg`, etc.) directly into
`QuerySpec.pipeline`, or Gemini reasons over the raw returned rows when writing the final answer.
`calculation.py` is available for a future Python-side calculation step but isn't wired in.

## Flow 1: `/ask` slash command

```mermaid
sequenceDiagram
    participant User as Slack user
    participant Bolt as Slack Bolt (app.command)
    participant H as handlers.handle_ask_command
    participant AC as access_control.is_authorized
    participant P as pipeline.answer_question
    participant Q as quota_tracker
    participant SC as schema_context.build_schema_context
    participant G as GeminiClient
    participant V as validator.validate_query_spec
    participant E as executor.execute_query_spec
    participant M as MongoDB
    participant L as audit.log_query_event

    User->>Bolt: /ask how many orders used a wallet?
    Bolt->>H: command dict {text, user_id, channel_id}
    H->>H: ack()
    H->>AC: is_authorized(channel_id, user_id)
    alt not authorized
        AC-->>H: False
        H-->>User: "Sorry, you're not authorized..." (visible denial)
    else authorized
        AC-->>H: True
        H->>H: strip/validate question text
        H->>P: answer_question(question, user_id=, channel_id=)
        P->>Q: is_over_budget()
        alt over daily budget
            Q-->>P: True
            P->>L: log_query_event(error="daily_budget_exceeded")
            P-->>H: "I've hit my daily question budget..."
        else under budget
            Q-->>P: False
            P->>SC: build_schema_context()
            SC-->>P: schema text (or "No schema information is available yet.")
            P->>G: generate_query_spec(question, schema_context)
            G->>Q: record_call()
            G->>G: _call_with_retry(...) [retries 429/5xx w/ backoff]
            Note over G: Gemini API call
            G-->>P: QuerySpec or QueryError
            alt QueryError (LLM says "can't answer this")
                P->>L: log_query_event(error=...)
                P-->>H: the error message, verbatim
            else QuerySpec returned
                P->>V: validate_query_spec(spec, allowed_collections)
                alt validation fails (banned op / disallowed collection)
                    V-->>P: raises QueryValidationError
                    P->>L: log_query_event(error=...)
                    P-->>H: "I can't run that query: ..."
                else valid
                    V-->>P: validated spec (limit clamped)
                    P->>E: execute_query_spec(get_db(), spec)
                    E->>M: find / aggregate / count
                    M-->>E: raw documents
                    E-->>P: JSON-safe rows (ObjectId/datetime -> str)
                    alt db error
                        P->>L: log_query_event(error=...)
                        P-->>H: "I ran into a database error..."
                    else no rows
                        P->>L: log_query_event(row_count=0)
                        P-->>H: "I didn't find any data..."
                    else rows found
                        P->>G: generate_answer(question, rows)
                        G->>Q: record_call()
                        G-->>P: natural-language answer
                        P->>L: log_query_event(row_count=, answer=)
                        P-->>H: answer text
                    end
                end
            end
        end
        H-->>User: respond(answer)
    end
```

## Flow 2: `@mention` and direct message

Same pipeline, different entry/exit shape. Two differences from `/ask`:
- Unauthorized access is **silent** (no reply at all), not a visible denial — deliberate, so the
  bot doesn't reveal it exists in channels it's not allowed to answer in.
- `@mention` replies in-thread (`thread_ts=event["ts"]`); DM replies directly in the DM channel.

```mermaid
sequenceDiagram
    participant User as Slack user
    participant Bolt as Slack Bolt (app.event)
    participant H as handlers.handle_mention / handle_dm
    participant AC as access_control.is_authorized
    participant P as pipeline.answer_question

    User->>Bolt: @bot how many orders...? (or a DM)
    Bolt->>H: event dict {text, user, channel, channel_type, ts, bot_id}
    Note over H: handle_dm also filters out non-DM events and bot-authored messages
    H->>AC: is_authorized(channel_id, user_id)
    alt not authorized
        AC-->>H: False
        H-->>H: return (no reply sent)
    else authorized
        AC-->>H: True
        Note over H: handle_mention strips the <@BOTID> mention text first
        H->>P: answer_question(question, user_id=, channel_id=)
        Note over P: identical to the /ask flow from here on
        P-->>H: answer text
        H-->>User: say(text=answer[, thread_ts=event.ts])
    end
```

## Function reference

Grouped by module, in call order for a typical successful request. `*` = private/internal
(leading underscore), not part of the module's public API.

### `app/main.py`
| Function | Does |
|---|---|
| `main()` | Entrypoint. Calls `configure_logging`, builds the Slack `App`, calls `register_handlers`, starts `SocketModeHandler`. Logs and re-raises on fatal startup errors. |

### `app/slack/handlers.py`
| Function | Does |
|---|---|
| `register_handlers(app)` | Registers the three handlers below on the Bolt `app`. |
| `handle_mention(event, say)` | `app_mention` handler. Checks `is_authorized`, strips the `<@BOTID>` mention via `_strip_mention`, calls `answer_question`, replies in-thread. |
| `handle_dm(event, say)` | `message` handler, filtered to DMs only (`channel_type == "im"`, not bot-authored). Checks `is_authorized`, calls `answer_question`, replies. |
| `handle_ask_command(ack, respond, command)` | `/ask` handler. Acks immediately, checks `is_authorized` (visible denial if not), validates the question isn't empty, calls `answer_question`, responds. |
| `*_strip_mention(text)` | Regex-strips `<@USERID>` mention markup from `@mention` event text. |

### `app/slack/access_control.py`
| Function | Does |
|---|---|
| `is_authorized(channel_id, user_id)` | Returns `True` unless `SLACK_ALLOWED_CHANNEL_IDS`/`SLACK_ALLOWED_USER_IDS` are set and the caller isn't in the list. Empty lists = open to everyone. |

### `app/rag/pipeline.py` — the orchestrator
| Function | Does |
|---|---|
| `answer_question(question, gemini=None, *, user_id=None, channel_id=None)` | The whole request lifecycle: budget check -> schema context -> Gemini query generation -> validation -> execution -> Gemini answer generation, with audit logging and a user-facing string return at every exit point (never raises to the caller). |
| `*_log(**kwargs)` | Local closure inside `answer_question`; fills in `question`/`user_id`/`channel_id`/`duration_ms` and calls `log_query_event`. |

### `app/llm/quota.py`
| Function | Does |
|---|---|
| `QuotaTracker.__init__(daily_budget)` | Sets up an in-process, thread-safe daily counter. |
| `QuotaTracker.record_call()` | Increments today's call count (resets first if the day rolled over). Called once per Gemini API call from inside `GeminiClient._call_with_retry`. |
| `QuotaTracker.is_over_budget()` | `True` if today's count >= budget (budget `<= 0` means unlimited). Checked by `pipeline.answer_question` before calling Gemini at all. |
| `QuotaTracker.remaining_budget()` | Calls remaining today, or `-1` if unlimited. |
| `*_reset_if_new_day()` | Resets the counter when the calendar day changes. |
| `quota_tracker` | Module-level singleton instance, constructed from `settings.gemini_daily_call_budget`. |

### `app/rag/schema_context.py`
| Function | Does |
|---|---|
| `build_schema_context(summary_path, annotations_path)` | Reads `schema_summary.json` + optional `schema_annotations.json`, formats them into the prompt text Gemini sees. Returns `"No schema information is available yet."` if the summary is missing, empty, or corrupt — **this is the exact message behind the `error` field you saw in production**, meaning `schema_summary.json` doesn't exist (or is unreadable) wherever the bot process is running. |
| `*_load_json(path)` | Loads a JSON file, returning `None` for a missing file *or* malformed/empty content (catches `JSONDecodeError` — this was a bug fixed recently; it used to raise and crash the whole pipeline). |

### `app/llm/gemini_client.py`
| Function | Does |
|---|---|
| `GeminiClient.__init__(model_name=None)` | Builds the `google.genai.Client`, reads retry settings from `app.config.settings`. |
| `GeminiClient.generate_query_spec(question, schema_context)` | Sends `_QUERY_PROMPT` to Gemini, parses the JSON reply into a `QuerySpec` or `QueryError`. |
| `GeminiClient.generate_answer(question, rows)` | Sends `_ANSWER_PROMPT` (question + the actual result rows as JSON) to Gemini, returns the natural-language reply text. |
| `*GeminiClient._call_with_retry(fn)` | Wraps any Gemini API call: records a quota call, retries on 429/5xx with exponential backoff + jitter up to `max_retries`, re-raises immediately on non-retryable errors or after retries are exhausted. |
| `*_is_retryable(exc)` | `True` if `exc` is a `google.genai.errors.APIError` with a 429/500/502/503/504 status code. |
| `*_extract_json(text)` | Regex-extracts the first `{...}` block from Gemini's raw text response and `json.loads`s it. |

### `app/rag/query_spec.py`
| Model | Does |
|---|---|
| `QuerySpec` | Pydantic model: `collection`, `operation` (`find`/`aggregate`/`count`), `filter`, `pipeline`, `projection`, `sort`, `limit`. What Gemini is asked to produce instead of raw MQL. |
| `QueryError` | `{error: str}` — what Gemini returns when it can't answer the question from the available schema. |

### `app/rag/validator.py` — the safety gate
| Function | Does |
|---|---|
| `validate_query_spec(spec, allowed_collections, max_limit=200)` | Rejects any collection not in the allow-list, any operation outside `find`/`aggregate`/`count`, and any use of `$where`/`$function`/`$accumulator`/`$merge`/`$out` (including inside `$lookup` targets) anywhere in the filter/pipeline/projection. Clamps `limit` into `[1, max_limit]`. Raises `QueryValidationError` on rejection. |
| `*_find_violation(value, allowed_collections)` | Recursively walks the filter/pipeline/projection structure looking for banned operators or disallowed `$lookup` targets. |

### `app/db/executor.py`
| Function | Does |
|---|---|
| `execute_query_spec(db, spec, timeout_ms=5000)` | Runs the **validated** spec against MongoDB: `count_documents`, `find(...).limit(...).max_time_ms(...)`, or `aggregate([...pipeline, {"$limit": ...}])` depending on `spec.operation`. |
| `*_to_jsonable(value)` | Recursively converts `ObjectId`/`datetime` to strings so results can be JSON-serialized (for both the Gemini answer prompt and audit logs). |

### `app/db/mongo.py`
| Function | Does |
|---|---|
| `get_client()` | Lazily creates and caches a single `MongoClient` for the process. |
| `get_db()` | Returns `get_client()[settings.mongodb_db_name]`. |

### `app/audit/logger.py`
| Function | Does |
|---|---|
| `configure_logging(level="INFO")` | Attaches a JSON-formatting stdout handler to the `"audit"` logger, called once at startup in `main()`. |
| `log_query_event(**fields)` | Logs one structured JSON record per question (question, user/channel IDs, the `QuerySpec` if any, error, row count, duration, answer). This is the exact log format you're seeing in production. |
| `*_JsonFormatter.format(record)` | Turns a `LogRecord`'s `.event` dict (plus timestamp/level) into a single JSON line. |

### Not in the live request path (CLI-only tools)
| Function | Does |
|---|---|
| `app/db/introspect.py::main()` / `profile_collection()` | Samples MongoDB collections, writes `schema_summary.json`. Run manually, not by the bot process. **This is what's missing in your production environment right now.** |
| `app/db/seed.py::seed_orders()` | Demo-data generator for the `orders` collection. Dev-only. |
| `app/rag/calculation.py` | Pure math helpers, tested but not currently called from `pipeline.py` (see note above the component diagram). |
