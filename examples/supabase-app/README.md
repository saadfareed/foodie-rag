# Sample host application — one-time-code sign-in + the chat plugin

A small web app that signs users in with an **emailed one-time code**, resolves them to a role, and
embeds the assistant as a chat widget. It exists to show the one thing a host integration has to
get right: **the browser never decides who it is or what it may read.**

Zero npm dependencies. Node 20.6+ (for `--env-file`).

```
examples/supabase-app/
  otp.mjs              email one-time codes — hashing, TTL, attempt limits, throttling, delivery
  server.mjs           the host app — sign-in, session cookie, /api/chat-token
  public/              the dashboard the widget is embedded on
  supabase/schema.sql  optional: links a Supabase account to a MongoDB user (password path only)
```

`otp.mjs` is written to be lifted into your own application as-is. Every rule in it is commented
with what it is preventing.

## What talks to what

```
  browser ──email──► server.mjs ──► gateway /v1/identity/lookup ──► user_id + role
  server.mjs ──one-time code by email──► browser
  browser ──code──► server.mjs                       (hashed, single-use, attempt-limited)
  browser ◄──httpOnly session cookie── server.mjs    (holds user_id + role, nothing else)
  browser ──cookie──► /api/chat-token
                        └─ WIDGET_API_KEY + user_id + role ──► gateway /v1/session
  browser ◄──short-lived chat token───────────────────────────── gateway
  browser ──question──► gateway /v1/ask ──► the RAG pipeline ──► MongoDB
```

The role comes from `users.usertype` in MongoDB — a field the gateway already scopes on — so
customers and vendors are exactly who the data says they are. `users.email` is read only by
`app/db/identity.py`; the gateway's field policy hides it from the model entirely, so nobody can
ask the bot for a contact list.

The browser never sees `WIDGET_API_KEY`, and cannot assert a role anywhere in this flow. There is
nowhere for one to land: `/api/chat-token` reads no request body at all.

## Run it

### Quickest — demo mode, no Supabase project

```bash
cd examples/supabase-app
cp .env.example .env
```

Then in `.env`:

```
SESSION_SECRET=<node -e "console.log(require('crypto').randomBytes(32).toString('base64url'))">
WIDGET_API_KEY=<one of the gateway's WIDGET_API_KEYS secrets>
DEMO_MODE=true
```

Start the gateway (from the repository root) and then this app:

```bash
python -m app.api.main
```

```bash
npm start --prefix examples/supabase-app
```

Open http://127.0.0.1:3000 and sign in with a seeded address — `usr-00031@example.test` is a
vendor, `usr-00002@example.test` a customer (run `python -m app.db.seed_users` to create them). In
console mode the code is printed to this server's log *and* shown on the page, so no email provider
is needed.

Sign in as each in turn and ask the same question. The answers differ because the role does — that
is the guardrail working.

> Console mode prints codes and hands them to the browser, which also reveals whether an address
> exists. It is a demo mode. Set `OTP_EMAIL_PROVIDER=resend` or `sendgrid` with an API key to send
> for real; then no code is ever returned to the browser.

> Demo mode adds buttons that assert a role without proving anything, exactly like the Slack
> adapter's `/login`. It is announced loudly at startup and must never be enabled anywhere real.

### With a real Supabase project

1. Create a project, then run [`supabase/schema.sql`](supabase/schema.sql) in the SQL editor. It
   creates `public.app_users`, enables RLS with a read-own-row policy and **no write policy at
   all**, and adds a trigger so every new sign-up gets an unscoped row.
2. Create a user (Authentication → Users), then point it at a vendor that exists in your MongoDB
   `users` collection:
   ```sql
   update public.app_users set vendor_id = 'USR-00031' where id = '<auth user uuid>';
   ```
3. Fill in `SUPABASE_URL`, `SUPABASE_ANON_KEY` and `SUPABASE_SERVICE_ROLE_KEY` in `.env`, set
   `DEMO_MODE=false`, and restart.

**Why `app_users` and not `user_metadata`:** a signed-in user can write their own `user_metadata`
through Supabase's API. Using it as the source of truth for data scope would let any user
re-scope themselves to another vendor. `server.mjs` falls back to it only for the quick-start
path, and says so.

**Why the access token is verified by asking Supabase** (`GET /auth/v1/user`) rather than by
checking the JWT locally: Supabase issues HS256 on some projects and asymmetric keys on others, so
a locally-pinned algorithm is a bug waiting for a key migration — and asking also catches a
revoked session, which signature checking never does.

## Adapting it to your own app

If you already have login, the whole integration is `POST /api/chat-token` in
[`server.mjs`](server.mjs) — about thirty lines — plus the script tag injected in
[`public/app.js`](public/app.js). Assert the role your application already knows and you are done.

If you don't, take [`otp.mjs`](otp.mjs) and the two `/api/auth/*-code` routes as well.

- [`docs/web-plugin.md`](../../docs/web-plugin.md) — endpoint contract, widget options, production
  checklist.
- [`docs/authorization.md`](../../docs/authorization.md) — the role policy, and what a one-time
  code has to get right.
