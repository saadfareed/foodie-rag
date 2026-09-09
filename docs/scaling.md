# Running more than one instance

Every stateful guardrail in this codebase — the answer cache, the three per-conversation caches,
the rate limiter, the daily Gemini budget, the circuit breaker, generated reports, and `/login`
sessions — used to be a module-level dict. That made a second replica not just unhelpful but
*wrong*, and wrong in the quietest possible way.

`app/state/` is the seam that fixes it. One environment variable switches where the state lives.

```bash
STATE_BACKEND=redis
REDIS_URL=redis://localhost:6379/0
pip install redis
```

Both entrypoints refuse to start if `STATE_BACKEND=redis` and `REDIS_URL` is empty. That is
deliberate: silently falling back to in-process state would leave a cluster that starts cleanly,
serves every request successfully, and enforces none of its limits.

---

## What actually breaks without it

Not "the cache is less effective". Each of these is a limit that still reports itself as enforced
while enforcing something else:

| Guardrail | With per-replica state, across N replicas |
|---|---|
| `USER_RATE_LIMIT_PER_MINUTE` | Admits **N×** the configured rate. Every rejection message is still accurate; the number being enforced is not the one configured. |
| `GEMINI_DAILY_CALL_BUDGET` | Spends **N×** the budget, then meets Google's real 429 — the raw upstream error this budget exists to avoid ever showing anyone. |
| Clarification / conversation context | A follow-up handled by a different replica finds nothing pending and is answered as an unrelated question. Users experience this as the bot forgetting mid-sentence, intermittently. |
| Context-switch confirmation | The user answers "yes" to a question the replica that receives it never asked. |
| `/login` vendor session | The next message may be answered **unscoped** — returning *more* rows than the user should see. The worst direction for this one to fail in. |
| Report downloads | The URL only works on the replica that generated the file; behind a load balancer it 404s roughly (N−1)/N of the time. |
| Circuit breaker | Each replica rediscovers the same outage, paying the full retry/timeout cost once per replica per cooldown. |
| Answer cache | Misses that should have hit. The only one on this list that is merely wasteful. |

The Gemini free-tier quota is per *project*, not per process, so the budget row is the one that
bites first and hardest.

---

## How it works

```
guardrail (answer_cache, rate_limiter, quota_tracker, …)
   └── app/state/store.py   TtlStore  — TTL + LRU, key encoding
         └── app/state/backend.py   StateBackend  — 7 primitives
               ├── InMemoryBackend   (default; the original behaviour, exactly)
               └── RedisBackend      (shared; lazily imported)
```

The primitive set is small and chosen so every operation a guardrail needs is **atomic in Redis**.
Read-modify-write over `get`/`set` would be a race — two replicas both read 19, both write 20, and
21 calls have been spent against a budget of 20 — so counters go through `incr` and the sliding
window goes through `allow_in_window`, each a single Lua script Redis runs to completion.

The window takes its clock from Redis's own `TIME`, not from the caller. Replicas do not share a
clock, and a window assembled from several machines' opinions of "now" is a window whose length
nobody can state.

---

## Deploying more than one

1. `STATE_BACKEND=redis`, `REDIS_URL=…`, `pip install redis` on every replica.
2. Point them at the same Redis. `STATE_KEY_PREFIX` (default `ragchat`) namespaces every key, so
   sharing an instance with something else is fine.
3. Run **one** Slack replica regardless. Socket Mode delivers each event once, but a second
   connection means both replicas receive and answer the same message. The web gateway is the
   part that scales horizontally.
4. Set a `maxmemory-policy` on Redis. Everything written here has a TTL, so `noeviction` is safe
   and `allkeys-lru` is acceptable; what must not happen is a policy that evicts keys with TTLs
   set while leaving the ones without.

## Verifying it

The cross-replica behaviour has its own test suite, skipped unless you point it at a Redis:

```bash
REDIS_TEST_URL=redis://127.0.0.1:6379/15 pytest tests/test_redis_integration.py
```

It constructs two of each guardrail over one Redis — two `QuotaTracker`s, two `RateLimiter`s, two
`CircuitBreaker`s, two `FileStore`s — and asserts they behave as one. Use a scratch database; each
test clears its own key prefix and never the database. CI runs it against a service container.

## Operational notes

- **A deploy that changes a stored shape is a miss, not a crash.** `TtlStore.get_json` treats
  unreadable JSON as absent, so a rolling deploy where old and new replicas write different
  encodings costs a recomputation rather than raising on the request path.
- **Nothing here is a durable store.** Every key has a TTL and losing Redis loses cached answers,
  in-flight clarifications and pending downloads — annoying, never incorrect. Business data lives
  in MongoDB and is never written here.
- **The report store holds bytes.** `WIDGET_MAX_CACHED_FILES` × a report's size is the memory it
  can occupy; with Redis that is Redis's memory, not the app's.
- **Startup says which mode you are in.** `STATE_BACKEND=memory` logs a `startup_state_warning`
  naming exactly what is not shared. If you are running more than one instance and see that line,
  the limits are not doing what the configuration says.
