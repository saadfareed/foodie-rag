# The web chat plugin

Embed the assistant in any web application as a chat widget — one `<script>` tag, no build step,
and no change to how questions are answered.

This is a **second adapter**, not a second product. `app/api/` sits beside `app/slack/` on the
same pipeline: the same agent graph, the same validator, the same field policy, the same message
catalogue. Both can run at once against one database. Nothing below the adapter knows which one a
question arrived through.

```
                     ┌──────────────────────────┐
  Slack  ──────────► │                          │
                     │   app/rag/pipeline.py    │ ──► agent graph ──► MongoDB
  Browser ─► app/api/│      answer_question     │
                     └──────────────────────────┘
```

---

## The security model

A browser will send whatever it is told to send. Slack states who is speaking; a browser asserts
it. So the identity a question is answered under never comes from the browser:

```
1.  browser ─── signs in ──────────────────────────► your identity provider (Supabase, SSO, …)
2.  browser ─── proves it ─────────────────────────► your app's server
3.  your server ── "user U may see vendor V" ──────► POST /v1/session   (with WIDGET_API_KEY)
4.  your server ◄── short-lived session token ─────  gateway
5.  browser ─── question + session token ──────────► POST /v1/ask
```

Two credentials, deliberately different in kind:

| | `WIDGET_API_KEYS` entry | Session token |
|---|---|---|
| Held by | your app's **server** | the **browser**, in memory |
| Lifetime | until rotated | `WIDGET_SESSION_TTL_SECONDS` (default 15 min) |
| Grants | minting a session for any user, any scope | asking questions as one user, at one scope |

**The browser cannot choose its own role**, because it never holds the credential that could. A
`role` in an `/v1/ask` body is inert — the gateway reads it only from the signed token, turns it
into a `Principal`, and `app/agents/graph.py` forces the matching filter onto every generated
query in code. There is no phrasing, and no request shape, that gets around it.

`WIDGET_ALLOWED_SESSION_ROLES` limits which roles a host key may assert, and `admin` is not in the
default — see [authorization.md](authorization.md).

The same reasoning applies to the conversation id. It is derived from the token
(`web:{tenant}:{principal}`), not accepted from the request — otherwise one user could attach to
another's pending clarification, follow-up context and per-channel answer cache by guessing a
string.

---

## Running the gateway

```bash
pip install -r requirements.txt
python -m app.api.main
```

Then open **http://127.0.0.1:8000/**. What that page shows depends on who is looking, and the
gateway decides it rather than the page: signed out it is a sign-in form, signed in it is the chat
scoped to that account, and the endpoint reference and embedding guide are stripped from the HTML
entirely for anyone who isn't an `admin`. The gateway has no other pages: it answers questions and
serves `widget.js`.

To try the chat there without wiring up a host application first:

```bash
python -m app.db.seed_users                       # demo accounts, and one admin
WIDGET_DEV_PLAYGROUND=true python -m app.api.main
```

Sign in with an **email and password** of a row in your `users` collection, or create one from the
same screen — the sign-up tab writes a new row (name, account type, city, and optionally the
browser's current location) and signs you straight in. If your `users` collection predates these
fields, give the existing rows credentials first:

```bash
python -m app.db.backfill_credentials          # preview; writes nothing
python -m app.db.backfill_credentials --apply  # every user gets <user_id>@example.test / test123
```

Both the sign-up form and that CLI **write to MongoDB**, which is the one thing the rest of this
application never does — `MONGODB_URI` is meant to be a read-only credential, and against one they
fail with a message saying so. `app/db/accounts.py` is the only module in `app/` that writes;
nothing on the question path can reach it. The role comes from that account's `usertype`, never from
anything typed into the page — asking a person for their own `USR-00031` was asking the wrong
question, and a role you pick from a dropdown is a role you chose rather than one you hold. The
widget then answers under exactly the scope that role gets in production: the same token, the same
forced filters, the same live-account check.

`admin` is not in the default `WIDGET_ALLOWED_SESSION_ROLES`, so signing in as the seeded operator
account needs `WIDGET_ALLOWED_SESSION_ROLES=customer,vendor,admin` — deliberately an explicit act,
in development as well as anywhere else.

It mints sessions **without a host-app key**, which is a deliberate hole in the one credential
boundary this service owns. So it is off by default, refused for any caller that isn't on this
machine, still bound by `WIDGET_ALLOWED_SESSION_ROLES`, and announced loudly at startup. It shares
its checks with `/v1/session` precisely so a playground session can never be more permissive than a
real one — what works here works in production, and what fails here would have failed there. The
sign-in cookie it sets is signed and `HttpOnly` for the same reason: next to a password field, a
cookie anyone could edit to say `admin` would be a lie.

Configuration (all in `app/config.py`, like everything else):

| Variable | Default | Notes |
|---|---|---|
| `WIDGET_API_KEYS` | — | **Required.** Comma-separated. Each entry is `tenant:secret` or a bare `secret` — the `tenant:` prefix is only recognised when it is a plain identifier, so a secret containing a colon isn't truncated. Generate with `secrets.token_urlsafe`. |
| `WIDGET_JWT_SECRET` | — | **Required**, ≥32 chars. Signs session and download tokens. |
| `WIDGET_ALLOWED_ORIGINS` | *(open)* | Browser origins allowed to call the gateway. Empty logs a warning at startup. |
| `WIDGET_SESSION_TTL_SECONDS` | `900` | How long a browser session token is valid. |
| `WIDGET_FILE_TTL_SECONDS` | `600` | How long a generated report stays downloadable. |
| `WIDGET_MAX_CACHED_FILES` | `200` | LRU bound on the in-memory report store. |
| `WIDGET_MAX_QUESTION_CHARS` | `2000` | Rejected before anything downstream sees it. |
| `WIDGET_API_HOST` / `WIDGET_API_PORT` | `127.0.0.1` / `8000` | Loopback by default — binding every interface is a deployment decision. |
| `WIDGET_DEV_PLAYGROUND` | `false` | Email/password sign-in and a live chat on the landing page, minting sessions with no host-app key. Loopback callers only. Local development; never anywhere real. |

Generate a signing key:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

The gateway **refuses to start** without a signing key or a host-app key. That is deliberate: a
gateway missing either starts fine, serves requests, and does no identity checking at all — which
looks exactly like working.

---

## Embedding it in your application

### 1. Add one endpoint to your backend

It answers `{"token": "...", "expires_in": 1800}` for whoever is logged in. This is the only code
you have to write, and the only place your `WIDGET_API_KEY` may appear.

```js
// Express, but the shape is the same in any language.
app.post("/api/chat-token", async (req, res) => {
  const user = await getLoggedInUser(req);           // your session, your rules
  if (!user) return res.status(401).json({ error: "Not signed in." });

  const response = await fetch(`${GATEWAY_URL}/v1/session`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${process.env.WIDGET_API_KEY}`,
    },
    body: JSON.stringify({ user_id: user.id, role: user.role }),
  });

  res.status(response.status).json(await response.json());
});
```

`role` is `admin`, `vendor` or `customer`. If your application has no login of its own,
[examples/supabase-app](../examples/supabase-app) ships a complete email one-time-code flow that
resolves an address to a user id and role via `/v1/identity/lookup`.

### 2. Add the script tag

```html
<script
  src="https://your-gateway.example/widget.js"
  data-gateway="https://your-gateway.example"
  data-token-endpoint="/api/chat-token"
  data-title="Ask your data"
  data-accent="#4f46e5"
></script>
```

| Attribute | Default | |
|---|---|---|
| `data-gateway` | — | **Required.** Where the gateway lives. |
| `data-token-endpoint` | `/api/chat-token` | Your endpoint from step 1. Same-origin, called with credentials. |
| `data-title` | `Ask your data` | Panel heading. |
| `data-greeting` | *(from `GET /v1/me`)* | First message. Unset, the widget asks the gateway for a greeting that names the user and what their role may ask about. |
| `data-accent` | `#4f46e5` | Any CSS colour. |
| `data-open` | `false` | `true` opens the panel on load. |

### 3. Optionally drive it from your own UI

```js
window.DataChat.ask("How many orders are pending?");
window.DataChat.open();
window.DataChat.close();
```

Deliberately narrow: there is no way to set a token or a scope from the page, because those come
from your server.

---

## API reference

### `GET /` — the landing page

A sign-in form, or the chat for whoever signed in. The endpoint reference and the embedding guide
are included only for an `admin`; everyone else is served a page that does not contain them.
Always served; only the minting is gated.

### `POST /v1/session` — server to server

```
Authorization: Bearer <one of WIDGET_API_KEYS>
{ "user_id": "USR-00031", "role": "vendor", "name": "Kifayat Foods" }
→ 200 { "token": "eyJ…", "expires_in": 1800 }
→ 400 { ... "error": "unknown_role" }        role isn't admin/vendor/customer
→ 403 { ... "error": "role_not_permitted" }  role isn't in WIDGET_ALLOWED_SESSION_ROLES
→ 401 { ... "error": "not_authenticated" }
```

`role` is required. There is deliberately no default: `admin` would grant everything on a typo,
and `customer` would silently mis-scope a vendor.

`name` is optional and is only what the widget greets this person with. Send it if you have it:
you are the only party that does, and without it the greeting simply has no name in it.

### `GET /v1/me` — browser

```
Authorization: Bearer <session token>
→ 200 { "user_id": "USR-00031", "role": "vendor", "name": "Kifayat Foods",
        "greeting": "Hi Kifayat Foods.\n\nAsk about your orders, ..." }
→ 401 { ... "error": "not_authenticated" }
```

What the widget opens with. The greeting is built here, not in the widget, because it is per-role:
telling a customer to "ask about your orders, customers or vendors" invites the one question their
role will refuse. Override it for your own wording with `data-greeting` on the script tag, which
skips this request entirely.

### `POST /v1/identity/lookup` — server to server

Exchanges an address your application has **already verified** for the identity it maps to, so you
don't need your own MongoDB credential to find out who someone is.

```
Authorization: Bearer <one of WIDGET_API_KEYS>
{ "email": "usr-00031@example.test" }
→ 200 { "found": true, "user_id": "USR-00031", "role": "vendor", "name": "Kifayat Foods" }
→ 200 { "found": false, "user_id": null, "role": null, "name": null }
```

It verifies nothing — proving control of the address is your job. An unknown address, a suspended
account and an unmapped `usertype` are one response, because any difference between them is a way
to test whether an address is registered. Rate-limited per key for the same reason.

### `POST /v1/ask` — browser

```
Authorization: Bearer <session token>
{ "question": "how many orders are pending?" }
→ 200 { "text": "...", "error": null, "file": null }
→ 200 { "text": "...", "error": null,
        "file": { "id": "…", "type": "csv", "filename": "report.csv", "url": "/v1/files/…?t=…" } }
→ 401 { "text": "Your chat session has expired…", "error": "session_expired", "file": null }
```

**Status codes follow one rule.** A *gateway* rejection (no token, expired token, empty or
oversized message) is a 4xx, because the request never became a question. A *pipeline* outcome
(rate limited, over budget, upstream down, no data) is **200 with `error` set**, because it is
the answer — "you're asking faster than I can keep up" is a sentence the user needs to read in
the chat, not a transport failure. Every reply carries `text` either way.

### `POST /v1/ask/stream` — browser

Same authentication, same scoping, same answer — delivered as Server-Sent Events while it is
built. This is what the widget uses.

```
Authorization: Bearer <session token>
{ "question": "how many orders are pending?" }
→ 200 text/event-stream

event: stage    data: {"text": "understanding"}
event: stage    data: {"text": "querying", "detail": "orders"}
event: stage    data: {"text": "writing"}
event: token    data: {"text": "42 orders are "}
event: token    data: {"text": "currently pending."}
event: result   data: {"text": "42 orders are currently pending.", "error": null, "file": null}
```

| Event | |
|---|---|
| `stage` | `understanding` → `locating` → `querying` (with the domain in `detail`) → `writing` → `report`. Most of a question's time is spent before the first token exists; a spinner for eight seconds and a spinner for two look identical. |
| `token` | A piece of the answer, **already redacted**. |
| `result` | The complete answer, plus any file. **This is the authoritative text.** |
| `error` | Something unhandled. Carries the catalogue's wording and a reference code. |

Two properties worth knowing before writing a client:

* **`result` is authoritative, tokens are a preview.** The result is the text that went through
  the finished-prose PII scan, the audit log and the answer cache. Progress events may be dropped
  under backpressure (a slow client fills a bounded queue, and progress is discarded so the worker
  thread is never blocked) — the result never is. The widget replaces the bubble with it.
* **A client that handles only `result` behaves exactly like a client of `/v1/ask`.** Streaming is
  additive.

Authentication failures happen *before* the stream opens, so they are ordinary 401s with a JSON
body — not a 200 carrying bad news.

**Streamed tokens do not bypass the PII scanner.** `scan_output_for_pii` runs on finished prose,
which for a stream would arrive long after a card number had been displayed; and re-scanning each
chunk on its own is no fix, because a card number split across two chunks matches neither half.
`app/security/output_scanner.py::StreamingRedactor` buffers and releases text only up to a point
where a match provably cannot straddle the cut — the last character no pattern can match. In
ordinary prose that is almost every character, so it stays a few characters behind the model
rather than holding the answer back. The invariant is stated in that module: **a new pattern must
use only the characters in `_UNSAFE_IN_A_MATCH`, or widen that set.**

### `GET /v1/files/{id}?t=…` — browser

Returns the report with `Content-Disposition: attachment` and `X-Content-Type-Options: nosniff`.
The token is a query parameter because the browser follows this URL itself and cannot attach a
header. It is a *different* token from the session one: a download URL ends up in history, the
address bar and referrers, and a session token has no business being there.

---

## The widget

`app/api/static/widget.js` — vanilla JavaScript, no dependencies, served by the gateway.

Two properties in it are load-bearing rather than stylistic:

* **Every piece of server text is written with `textContent`.** Answers are generated from
  database rows. Slack rendered them as text; a browser would happily render a stored value as
  markup, which turns one poisoned row into stored XSS on every page embedding this. There is no
  HTML-rendering path in the file at all, so there is nothing to forget to escape.
* **The session token lives in a closure variable and nowhere else** — not `localStorage`, not a
  cookie. It carries the scope the backend answers under, so an XSS in the *host* page shouldn't
  find a durable credential lying around. It dies with the tab and is re-fetched on expiry.

Everything renders in a shadow root, so the host application's CSS and the widget's cannot reach
each other in either direction.

---

## Before production

- [ ] Set `WIDGET_ALLOWED_ORIGINS` to the origins that actually embed the widget.
- [ ] Terminate TLS in front of the gateway. Session tokens are bearer credentials.
- [ ] Rotate `WIDGET_API_KEYS` by adding the new key, redeploying hosts, then dropping the old
      one. Both are accepted while both are listed.
- [ ] Set `GEMINI_DAILY_CALL_BUDGET` and `USER_RATE_LIMIT_PER_MINUTE`. A widget on a live page
      sees far more traffic than a Slack channel, and Google's free-tier quota is still the real
      throughput ceiling (see CLAUDE.md).
- [ ] If your host page sends CSP, allow the gateway origin in `script-src` and `connect-src`.
- [ ] Set `STATE_BACKEND=redis` before running more than one gateway replica — see
      [scaling.md](scaling.md). In-process state means each replica enforces its own rate limit and
      its own daily budget, and a download only works on the replica that generated it.
- [ ] If a proxy sits in front of the gateway, disable response buffering for
      `/v1/ask/stream` (the gateway sends `X-Accel-Buffering: no`, which nginx honours) — buffering
      turns a live stream back into a slow page load.

## Known limitations

- **One conversation per user.** The conversation id is derived from the principal, mirroring a
  Slack DM. `reset` starts a new topic. Multiple named threads would need a thread claim minted
  server-side, alongside the principal — and it must stay server-side, for the reason in the
  security model above.
- **Only the answer streams.** Stage events cover the rest of the pipeline, but the classify and
  per-domain query-generation calls are structured-output calls that are consumed whole; there is
  no partial JSON worth showing. In practice that is the first few seconds of a question.
- **A stream that fails after it starts cannot be retried.** Once text has reached the user,
  restarting under a fallback model would replay the answer from the beginning over what they have
  already read, so `StreamInterrupted` stops instead — reported as "try again shortly", with the
  partial answer left in place.
