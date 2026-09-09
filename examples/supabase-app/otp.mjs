/*
 * Email one-time-code sign-in.
 *
 * This lives in the *host application*, not in the gateway, and that is the architectural point:
 * the gateway answers data questions and never learns how anyone proved who they are. It is
 * handed a verified identity and a role, and that is all it needs.
 *
 * What a one-time code has to get right, and what each rule is actually preventing:
 *
 *   Codes are stored hashed          A readable store of live codes is a store of live sessions.
 *   Single use                       Otherwise a code lingering in an inbox is a reusable password.
 *   Short TTL                        The window in which a forwarded or shoulder-surfed code works.
 *   Attempt limit per code           Six digits is 1,000,000 guesses; unlimited attempts is none.
 *   Request throttle per address     Otherwise this is a free mailbox-flooding service.
 *   Constant-time comparison         A short-circuiting compare leaks the code a digit at a time.
 *   Identical response either way    "We sent a code" for every address, registered or not --
 *                                    anything else turns sign-in into a customer-list oracle.
 *
 * State is in memory here, which is right for one demo process and wrong for anything else: a
 * second replica would not recognise a code the first one issued. The gateway solved the same
 * problem with app/state (Redis); a real host application should do likewise.
 */

import { createHash, randomInt, timingSafeEqual } from "node:crypto";

const CODE_TTL_SECONDS = Number(process.env.OTP_CODE_TTL_SECONDS || 600);
const MAX_ATTEMPTS = Number(process.env.OTP_MAX_ATTEMPTS || 5);
const REQUESTS_PER_HOUR = Number(process.env.OTP_REQUESTS_PER_HOUR || 5);
const CODE_LENGTH = 6;

const EMAIL_PROVIDER = (process.env.OTP_EMAIL_PROVIDER || "console").toLowerCase();
const EMAIL_FROM = process.env.OTP_EMAIL_FROM || "login@example.test";
const EMAIL_API_KEY = process.env.OTP_EMAIL_API_KEY || "";
const APP_NAME = process.env.APP_NAME || "Northwind Portal";

/** email -> { hash, expiresAt, attempts } */
const pending = new Map();
/** email -> [timestamps] */
const requests = new Map();

function hashCode(email, code) {
  // Salted with the address so a code is only valid for the address it was sent to -- otherwise
  // one live code is a valid code for every pending sign-in.
  return createHash("sha256").update(`${email}:${code}`).digest("hex");
}

function withinRequestLimit(email) {
  const now = Date.now();
  const recent = (requests.get(email) || []).filter((t) => now - t < 3600_000);
  recent.push(now);
  requests.set(email, recent);
  return recent.length <= REQUESTS_PER_HOUR;
}

/**
 * Issue a code for `email`. Returns { sent, code } -- `code` is only populated in console mode,
 * where the caller logs it instead of emailing it.
 *
 * Deliberately does not report whether the address belongs to anyone. The caller checks that
 * separately and answers the browser identically either way.
 */
export async function requestCode(email, { exists }) {
  if (!withinRequestLimit(email)) {
    return { sent: false, throttled: true };
  }
  if (!exists) {
    // Nothing is sent and nothing is stored, but the caller still tells the browser "check your
    // email". An address that gets no message and an address that gets one must be
    // indistinguishable from the outside.
    return { sent: false, throttled: false };
  }

  // randomInt is CSPRNG-backed. Math.random() is not, and a predictable code is not a code.
  const code = String(randomInt(0, 10 ** CODE_LENGTH)).padStart(CODE_LENGTH, "0");
  pending.set(email, {
    hash: hashCode(email, code),
    expiresAt: Date.now() + CODE_TTL_SECONDS * 1000,
    attempts: 0,
  });

  await sendEmail(email, code);
  return { sent: true, throttled: false, code: EMAIL_PROVIDER === "console" ? code : undefined };
}

/**
 * Check a submitted code. Returns { ok, reason }.
 *
 * The entry is deleted on success *and* on running out of attempts, so a code is single-use and a
 * exhausted one cannot be ground down by requesting a fresh window of guesses against it.
 */
export function verifyCode(email, submitted) {
  const entry = pending.get(email);
  if (!entry) return { ok: false, reason: "no_code" };

  if (Date.now() > entry.expiresAt) {
    pending.delete(email);
    return { ok: false, reason: "expired" };
  }

  entry.attempts += 1;
  if (entry.attempts > MAX_ATTEMPTS) {
    pending.delete(email);
    return { ok: false, reason: "too_many_attempts" };
  }

  const expected = Buffer.from(entry.hash);
  const actual = Buffer.from(hashCode(email, String(submitted || "").trim()));
  // Compared as fixed-length hashes rather than raw codes, so timingSafeEqual's length
  // requirement is satisfied whatever the user typed -- and the comparison itself does not
  // short-circuit on the first wrong digit.
  const matches = expected.length === actual.length && timingSafeEqual(expected, actual);
  if (!matches) return { ok: false, reason: "mismatch" };

  pending.delete(email);
  return { ok: true };
}

/* ---- delivery ------------------------------------------------------------------------------ */

const SUBJECT = () => `Your ${APP_NAME} sign-in code`;
const BODY = (code) =>
  `Your sign-in code is ${code}. It expires in ${Math.round(CODE_TTL_SECONDS / 60)} minutes.\n\n` +
  `If you didn't ask for this, you can ignore this email.`;

async function sendEmail(email, code) {
  if (EMAIL_PROVIDER === "console") {
    // The demo default: no provider account needed, and the code goes to the server log where
    // whoever is running the demo can see it.
    //
    // Note this mode *does* break the "identical response either way" rule listed at the top of
    // this file -- the caller hands the code back to the browser so the demo can be completed,
    // which reveals whether an address exists. That is one more reason console mode is a demo
    // mode. With a real provider configured no code is ever returned to the browser.
    console.log(`\n  [OTP] code for ${email}: ${code}  (console mode -- no email was sent)\n`);
    return;
  }
  if (!EMAIL_API_KEY) {
    throw new Error(`OTP_EMAIL_PROVIDER=${EMAIL_PROVIDER} but OTP_EMAIL_API_KEY is not set`);
  }

  // Both providers are plain JSON over HTTPS, which is why this file has no dependencies. Adding
  // another is a third branch of the same shape.
  const request =
    EMAIL_PROVIDER === "resend"
      ? {
          url: "https://api.resend.com/emails",
          headers: { Authorization: `Bearer ${EMAIL_API_KEY}` },
          body: { from: EMAIL_FROM, to: [email], subject: SUBJECT(), text: BODY(code) },
        }
      : {
          url: "https://api.sendgrid.com/v3/mail/send",
          headers: { Authorization: `Bearer ${EMAIL_API_KEY}` },
          body: {
            personalizations: [{ to: [{ email }] }],
            from: { email: EMAIL_FROM },
            subject: SUBJECT(),
            content: [{ type: "text/plain", value: BODY(code) }],
          },
        };

  const response = await fetch(request.url, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...request.headers },
    body: JSON.stringify(request.body),
  });
  if (!response.ok) {
    // The provider's body can contain the recipient address; it goes to the server log, never to
    // the browser. The caller answers "check your email" regardless.
    console.error(`[otp] ${EMAIL_PROVIDER} rejected the send:`, response.status, await response.text());
    throw new Error("email_send_failed");
  }
}

export const config = {
  provider: EMAIL_PROVIDER,
  codeTtlSeconds: CODE_TTL_SECONDS,
  maxAttempts: MAX_ATTEMPTS,
  requestsPerHour: REQUESTS_PER_HOUR,
};
