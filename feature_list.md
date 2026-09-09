# Roadmap

What's shipped, what's deliberately deferred, and what's still a wishlist. Items move up this
file as they land — a roadmap that only ever grows is a wishlist with better formatting.

For *why* the deferred things are deferred, see the "out of scope" section in
[CONTRIBUTING.md](CONTRIBUTING.md).

---

## Shipped

| | Where |
|---|---|
| **Two front ends on one pipeline** — Slack bot (Socket Mode) and an embeddable web chat widget | [docs/web-plugin.md](docs/web-plugin.md) |
| **Role-based access** — admin / vendor / customer, forced onto every query in code | [docs/authorization.md](docs/authorization.md) |
| **Email one-time-code sign-in** — hashed, single-use, attempt-limited, throttled | [examples/supabase-app/otp.mjs](examples/supabase-app/otp.mjs) |
| **Password sign-in for the dev playground** — PBKDF2, per-record salt, equal-cost failures; the role comes from the account | [docs/authorization.md](docs/authorization.md) |
| **A role-aware landing page and greeting** — the widget opens with what *your* role can ask about; operator documentation is served only to admins | [docs/web-plugin.md](docs/web-plugin.md) |
| **Streaming answers** — stage events plus tokens, redacted incrementally | [docs/web-plugin.md](docs/web-plugin.md) |
| **Horizontal scaling** — every guardrail's state moves to Redis with one env var | [docs/scaling.md](docs/scaling.md) |
| **Per-role usage limits** — rate, daily questions, report size | [docs/authorization.md](docs/authorization.md) |
| **CSV / XLSX / PDF reports** with rule-chosen charts | [docs/architecture.md](docs/architecture.md) |
| **Conversational follow-ups** with an explicit context-switch confirmation | [CLAUDE.md](CLAUDE.md) |

The two items this file previously listed as future work — **MFA/OTP** and **granular RBAC** —
are both in that table now. The OTP flow uses email; SMS is a provider swap in one file.

---

## Next, in rough order of value per hour

1. **Immediate session revocation.** Today revocation is bounded by the session TTL (15 minutes),
   because every renewal re-checks the account. A `revoked_before:{tenant}:{sub}` timestamp in
   `app/state`, checked in `verify_session_token`, makes it instant — about forty lines, since the
   state layer already exists.
2. **Persistent chat history.** None today: conversation context is one turn and expires in five
   minutes. Client-side (`sessionStorage`, keyed by principal) is nearly free; server-side survives
   devices but means storing answers containing customer data, so retention and a deletion
   endpoint come before the code.
3. **Scheduled reports.** A cron that runs a saved question as a given principal and pushes the
   result. The pipeline already takes a `Principal` and returns an `AnswerResult` with file bytes,
   so this is mostly scheduling and delivery — the hard part is that a schedule outlives the
   session that created it, so the principal has to be re-resolved and re-checked at run time.
4. **Custom roles.** Three roles are hard-coded in `app/security/roles.py`. Manager / staff /
   auditor means moving `DOMAIN_ACCESS` and `forced_filter` from a module constant to per-tenant
   configuration — the shape is already right, the table just becomes data.
5. **Role-aware field policy.** Would let a vendor ask about their own customers' contact details
   in *chat*, not just in an export. Needs the principal threaded into `app/db/executor.py`; the
   trade is written up in `_CONTACT_FIELD_PATTERNS`.

---

## Bigger, and genuinely undecided

- **Anomaly detection and proactive alerts.** "Order volume is down 30% on a typical Tuesday."
  Needs a baseline model and a delivery channel, and it inverts the request/response shape the
  whole system is built on. Real value; a real project.
- **Forecasting.** Same shape as above, plus the honesty problem: a forecast presented in the same
  voice as a database answer will be read as one.
- **Cross-tenant anonymised benchmarking.** "How does my average order compare to similar
  businesses nearby?" The differential-privacy question ("how few businesses can a bucket contain
  before an aggregate identifies one?") is the whole feature; the query is trivial.
- **Multi-language.** Gemini can answer in the asker's language today. What doesn't come free is
  the deterministic half — `app/messages.py`, the intent-router regexes, the reset and yes/no
  phrases — all of which are English-only by construction and would need per-locale sets.
- **Vector search.** See [CONTRIBUTING.md](CONTRIBUTING.md#whats-out-of-scope-for-now) for where
  it would genuinely help and the one rule that matters if it is built (embed metadata, never
  rows). The current schema is small enough to fit in a prompt and no collection holds
  unstructured text, so it buys nothing yet.
- **Generative UI / interactive dashboards.** Slack Block Kit or a richer widget surface. Worth
  doing after chart selection is driven by more than the rules in `app/generators/charts.py`.
