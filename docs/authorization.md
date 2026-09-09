# Who can see what

Two questions, answered in two different places, both in code and never in a prompt:

| | Question | Where |
|---|---|---|
| **Fields** | Which columns may leave the system, for anyone? | [app/security/field_policy.py](../app/security/field_policy.py) |
| **Rows** | Which records may leave it, for *this person*? | [app/security/roles.py](../app/security/roles.py) |

---

## The roles

They map onto the `usertype` discriminator your data already carries — `1` is a customer, `2` is a
vendor, `3` is an operator — so a verified account yields both an identity and a role from one
lookup.

`3` exists only so an admin can *sign in* like anybody else: an address, a password, and a status
that can be suspended. It is deliberately not one of the two data domains — `app/agents/domains.py`
scopes `customers` to `usertype 1` and `vendors` to `usertype 2`, in code, so an operator row is
invisible to every question by construction. An admin asserted by a host application still needs no
row at all; both shapes are legitimate, which is why `POST /v1/session` skips the live-account check
for admins rather than relying on its answer.

| | orders | customers | vendors |
|---|---|---|---|
| **admin** | all | all | all |
| **vendor** | `vendor_id = self` | the customers who have ordered from them | `user_id = self` |
| **customer** | `customer_id = self` | `user_id = self` | the directory, unfiltered |
| **anonymous** | — | — | — |

**The default is deny.** Before roles existed there was one axis — `authenticated_vendor_id` —
and when it was absent no filter was applied at all, so "not signed in" meant "sees everything".
That was survivable while the only entrance was an allow-listed Slack channel and it is not
survivable now: a web session that failed to carry a scope would have been an admin session.
`ANONYMOUS` reads nothing, and every caller must produce a `Principal`.

The one deliberate permission is a customer browsing vendors. Name, city, category and rating are
how a marketplace works, and restricting it to "vendors you have already ordered from" would break
the first question a new customer asks. Their orders and every other customer stay private.

### The one rule that needs a query

A vendor may see the customers who ordered from them, and that set has to be read before it can be
enforced. `app/agents/graph.py::_resolve_authorized_customer_ids` runs a distinct aggregation over
that vendor's own orders, in code, before the fan-out — never as a model-generated `$lookup`, for
the same reason [enrichment](../app/agents/enrichment.py) isn't one: the query is identical every
time, so there is nothing for a model to decide.

It is capped by `RBAC_MAX_AUTHORIZED_IDS`, and past the cap the domain is **refused**, not
truncated. A truncated scope would answer "my customers in Karachi" from an arbitrary subset while
looking complete, which is worse than saying no.

The failure direction matters more than the happy path: an unresolved id list becomes
`{"$in": []}` — matching nothing — never an absent filter. `tests/test_roles.py` asserts exactly
that, because it is the one place where a missing value could plausibly read as "no restriction".

---

## Signing in

**The gateway does not authenticate anyone.** It answers data questions; a verified identity and a
role are inputs to it. Proving who someone is belongs to the application they are already using.
(The one exception is the development playground's own sign-in form, below — off by default and
loopback-only.)

```
1. browser  --(email)------------------------------->  your app
2. your app --(email)------------------------------->  gateway  POST /v1/identity/lookup
3. your app <--(user_id + role, or "not found")------  gateway
4. your app --(one-time code by email)-------------->  browser
5. browser  --(code)-------------------------------->  your app        → session cookie
6. browser  --(cookie)------------------------------>  your app  POST /api/chat-token
7. your app --(key + user_id + role)---------------->  gateway  POST /v1/session
8. browser  <--(short-lived chat token)-------------  your app
```

Steps 4–5 are the one-time code, and they live in your application. A complete, commented
implementation is [examples/supabase-app/otp.mjs](../examples/supabase-app/otp.mjs) — around 150
lines with no dependencies, which you can lift wholesale.

### Why `/v1/identity/lookup` exists

`users.email` is a field the field policy **denies to everything else**: the model never sees it,
the validator rejects a query naming it, and the executor strips it from every row. So no question
can reach it, however it is phrased. But something has to read it to answer "who is this address",
and [app/db/identity.py](../app/db/identity.py) is the single code path allowed to.

Exposing that as one narrow, host-key-authenticated endpoint is a much better trade than handing
every host application its own MongoDB credential. It:

- takes an **exact, normalised** address — never a regex, or "prove you own this" becomes "name
  anything that looks a bit like it";
- reads a fixed four-field projection;
- refuses accounts whose `status` isn't `active`, so a suspended vendor loses access immediately
  rather than when their token expires;
- returns the **same response** for an unknown address, a suspended account, and an unmapped
  `usertype` — any difference is a way to test whether an address is registered;
- is rate-limited per key, because it is the one place existence can be probed.

**It verifies nothing.** Proving control of the address is your application's job.

### The one place that *does* verify: `authenticate_password`

The development playground has its own sign-in form, so it needs a proof rather than an assertion.
`app/db/identity.py::authenticate_password` is it: an exact address lookup plus a PBKDF2-HMAC-SHA256
check against `users.password_hash` ([app/security/passwords.py](../app/security/passwords.py)).

The stored hash carries its own salt and its own iteration count, so two accounts sharing a password
do not share a hash and the cost factor can be raised later without invalidating anyone. The field
policy matches `password_hash` as a secret field, so its value is replaced before a row leaves the
executor — no question can reach one however it is phrased.

Every way it can fail is the same answer *and* the same cost: an unknown address still pays for a
key derivation, because a login form that refuses instantly for addresses that don't exist is an
account enumeration oracle with a nicer interface.

This is for the playground, not for your application. Moving authentication into the gateway would
make it an identity provider, which is a different product with different obligations — see the
one-time code above for the shape a real host application wants.

### What a one-time code has to get right

Each of these is in the sample implementation, and each is preventing something specific:

| | |
|---|---|
| Codes stored hashed, salted per address | A readable store of live codes is a store of live sessions |
| Single use | Otherwise a code sitting in an inbox is a reusable password |
| Short TTL | The window in which a forwarded code still works |
| Attempt limit per code | Six digits is 1,000,000 guesses; unlimited attempts is none |
| Request throttle per address | Otherwise sign-in is a free mailbox flooder |
| Constant-time comparison | A short-circuiting compare leaks the code a digit at a time |
| CSPRNG (`randomInt`, not `Math.random`) | A predictable code is not a code |
| Identical response either way | "Check your email" for every address, or sign-in is a customer-list oracle |

---

## Sessions

A session token carries `sub` (the account) and `role`. Both come from your server; neither can be
influenced by the browser.

- **An unknown or missing role is a rejected token**, never a defaulted one. A token minted by an
  older version of this service must not fall back to *any* role — the two available fallbacks are
  wrong in opposite directions.
- **`WIDGET_ALLOWED_SESSION_ROLES` limits what a host key may assert**, and `admin` is not in the
  default. A host key is a long-lived secret sitting on someone else's server, and leaking one
  should not include the ability to mint an unrestricted session. This is the one
  privilege-escalation path the gateway itself owns.
- **The conversation id includes the role.** The same person signed in as a customer and as an
  admin is answered from different rows, so they must not share a follow-up context — or an answer
  cache entry, which is why `Principal.cache_scope` is `role:user_id` rather than the bare id.
- **A session mint re-checks the account.** `WIDGET_VERIFY_ASSERTED_IDENTITY` (on by default)
  confirms an asserted vendor or customer is still a live row in `users` before issuing a token.
  This is offboarding rather than distrust of the host app: without it, a suspended vendor whose
  application still has them logged in keeps reading. One indexed lookup per sign-in, not per
  question; admin accounts have no row and are skipped.
- **Slack is the documented exception.** `SLACK_DEFAULT_ROLE` (default `admin`) is what a Slack
  user has before `/login`, preserving the behaviour those deployments already have — a Slack
  channel is gated by `SLACK_ALLOWED_CHANNEL_IDS` and everyone in one was previously answered
  unfiltered. Set it to `anonymous` if that isn't true of your channels.

---

## What is audited

Every answer logs `role` and `principal_id` alongside the question. When authorization decides
which rows an answer contains, "who sent the message" is not enough — the role that authorised it
is the thing an auditor asks for, and it is not recoverable from the question afterwards.

---

## Usage limits

Three, and they stop different things:

| Setting | Bounds | Stops |
|---|---|---|
| `USER_RATE_LIMIT_PER_MINUTE` + `RATE_LIMIT_BY_ROLE` | one identity, per minute | a burst — a script, a stuck retry loop |
| `USER_DAILY_QUESTION_LIMIT` | one identity, per UTC day | a slow drain — one question every 30s passes every per-minute check and still exhausts a free-tier quota by lunchtime |
| `GEMINI_DAILY_CALL_BUDGET` | *everyone*, per day, in Gemini calls | the workspace overrunning Google's quota |

`RATE_LIMIT_BY_ROLE` takes `role:limit` pairs (`admin:60,vendor:20,customer:10`). The flat setting
remains the master switch — at `0` limiting is off and a per-role entry cannot turn it back on, so
a deployment that deliberately disabled it doesn't silently regain it by naming a role.

`REPORT_MAX_ROWS_BY_ROLE` works the same way for generated reports, and `REPORT_MAX_ROWS` stays
the absolute ceiling — an override may only lower it, never raise it past the bound the render
pool was sized for. Rendering is the most expensive CPU on the request path, so once customers can
reach it, "how many rows may this role ask WeasyPrint for" is a load question, not a nicety.

The daily limit is keyed by `role:user_id`, so it follows the person rather than the channel they
happen to be asking from — and the same id under two roles counts separately, because they are
answered from different rows.

---

## Contact details in reports

`email` and `phone` are dropped from every row on the way out of MongoDB, so the model is never
shown them and no question can produce a contact list however it is phrased. That is the right
default and it stays the default.

`REPORT_INCLUDE_CONTACTS=true` opens one narrow exception: a **generated report** may carry those
columns, added in code after the answer, for rows the principal was already authorized to see.

The rule is not "which domains may this role read" — it is **"which domains has this role's own
filter already narrowed to people they have a relationship with"**:

| | orders | customers | vendors |
|---|---|---|---|
| **admin** | ✓ | ✓ | ✓ |
| **vendor** | — | ✓ their own customers | ✓ their own record |
| **customer** | — | ✓ their own record | ✗ **the directory** |

That last cell is the one that matters. A customer may read `vendors` *unfiltered* — deliberately,
it is a directory — so a contact column there would be every vendor's phone number in one
download. `tests/test_roles.py` asserts contact visibility is a strict subset of read access.

Two more limits fall out of the design: it only applies to rows that carry a `user_id` (an orders
table references people by id, and [enrichment](../app/agents/enrichment.py) resolves those to
names and drops the id on purpose), and the lookup is deliberately dumber than the sign-in one —
no matching, no searching, just "read these columns for these ids".

---

## Known gaps

- **Contact details are still invisible in *answers*.** `REPORT_INCLUDE_CONTACTS` covers exports
  only; the model never sees these fields, so "what is Ayesha's number?" has no answer. Making
  that work needs a *role-aware* field policy — threading the principal into
  `app/db/executor.py` — and the field policy is deliberately applied at a choke point where no
  role is in scope. The mechanism is a day's work; whether bulk contact access through a
  natural-language prompt is something you want is the real question.
- **Sessions cannot be revoked before they expire.** A JWT is stateless, so disabling an account
  stops new sign-ins — `WIDGET_VERIFY_ASSERTED_IDENTITY` sees to that — but leaves an already
  issued token valid for up to `WIDGET_SESSION_TTL_SECONDS` (default 15 minutes). Lower it and
  revocation tightens proportionally, at one indexed lookup per renewal. If you need it to be
  *immediate*, a `revoked_before:{tenant}:{sub}` timestamp in `app/state`, checked in
  `verify_session_token`, is about forty lines — the state layer is already there.
- **No persistent chat history.** Conversation context is one turn and expires in
  `CONVERSATION_CONTEXT_TTL_SECONDS`. Reopening the widget tomorrow starts fresh.
