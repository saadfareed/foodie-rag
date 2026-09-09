/*
 * A small host application: Supabase for identity, this server for sessions, and the chat plugin
 * embedded on the dashboard.
 *
 * Zero dependencies on purpose. The interesting part of a host integration is about thirty lines
 * (`/api/chat-token` below), and burying it in a framework makes it look harder than it is.
 *
 * Two ways in, and the same outcome either way -- a session cookie this server signed, holding a
 * user id and a role:
 *
 *   OTP        browser --(email)--> this server --> gateway /v1/identity/lookup --> user + role
 *              this server emails a one-time code; browser returns it; cookie is set
 *   Supabase   browser --(access token, once)--> this server --(verify with Supabase)--> ok
 *
 * and then, for every question:
 *
 *   browser --(cookie)--> /api/chat-token --(WIDGET_API_KEY + user_id + role)--> gateway
 *   browser <--(short-lived chat token)------------------------------------------ gateway
 *
 * The browser never sees WIDGET_API_KEY or the Supabase service-role key, and never gets to say
 * what role it holds. That last one is the whole boundary: the role comes from the `users`
 * collection via the gateway's identity lookup, or from Supabase, and is asserted by this server.
 * A browser that posts its own role anywhere in this flow is ignored -- there is nowhere for it
 * to land, because /api/chat-token reads no request body at all.
 */

import { createHmac, timingSafeEqual, randomUUID } from "node:crypto";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { dirname, extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

import { config as otpConfig, requestCode, verifyCode } from "./otp.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const PUBLIC_DIR = join(HERE, "public");

const PORT = Number(process.env.PORT || 3000);
const SUPABASE_URL = (process.env.SUPABASE_URL || "").replace(/\/+$/, "");
const SUPABASE_ANON_KEY = process.env.SUPABASE_ANON_KEY || "";
const SUPABASE_SERVICE_ROLE_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY || "";
const GATEWAY_URL = (process.env.GATEWAY_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");
const WIDGET_API_KEY = process.env.WIDGET_API_KEY || "";
const SESSION_SECRET = process.env.SESSION_SECRET || "";
const SESSION_TTL_SECONDS = Number(process.env.SESSION_TTL_SECONDS || 3600);
// Lets the example run before a Supabase project exists. Off unless explicitly enabled, and
// announced loudly at startup, because "sign in as whoever you like" is a demo affordance that
// would be a catastrophe if it survived a copy-paste into something real.
const DEMO_MODE = process.env.DEMO_MODE === "true";

const COOKIE_NAME = "host_session";

/* ---- session cookie ---------------------------------------------------------------------- */

function sign(value) {
  return createHmac("sha256", SESSION_SECRET).update(value).digest("base64url");
}

function makeSessionCookie(session) {
  const payload = Buffer.from(
    JSON.stringify({ ...session, exp: Math.floor(Date.now() / 1000) + SESSION_TTL_SECONDS })
  ).toString("base64url");
  const cookie = `${payload}.${sign(payload)}`;
  // httpOnly so an XSS in this page can't read it; SameSite=Lax so another site can't ride it.
  // `Secure` is conditional only because this example runs on plain http://localhost.
  const flags = ["HttpOnly", "SameSite=Lax", "Path=/", `Max-Age=${SESSION_TTL_SECONDS}`];
  if (process.env.NODE_ENV === "production") flags.push("Secure");
  return `${COOKIE_NAME}=${cookie}; ${flags.join("; ")}`;
}

function readSession(request) {
  const raw = (request.headers.cookie || "")
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(`${COOKIE_NAME}=`));
  if (!raw) return null;

  const [payload, signature] = raw.slice(COOKIE_NAME.length + 1).split(".");
  if (!payload || !signature) return null;

  const expected = Buffer.from(sign(payload));
  const provided = Buffer.from(signature);
  if (expected.length !== provided.length || !timingSafeEqual(expected, provided)) return null;

  try {
    const session = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
    return session.exp > Math.floor(Date.now() / 1000) ? session : null;
  } catch {
    return null;
  }
}

/* ---- Supabase ---------------------------------------------------------------------------- */

/*
 * Verified by asking Supabase rather than by validating the JWT locally. Slower by one request,
 * and correct for every project: Supabase issues HS256 tokens on some projects and asymmetric
 * ones on others, and a locally-pinned algorithm is a bug that only shows up after a key
 * migration. This also picks up a revoked session, which signature checking alone never does.
 */
async function fetchSupabaseUser(accessToken) {
  const response = await fetch(`${SUPABASE_URL}/auth/v1/user`, {
    headers: { apikey: SUPABASE_ANON_KEY, Authorization: `Bearer ${accessToken}` },
  });
  if (!response.ok) return null;
  return response.json();
}

/*
 * The row-level scope, from a table this application controls -- not from anything the user can
 * edit. `user_metadata` is a fallback for the quick-start path, and it is worth knowing that a
 * signed-in user *can* write their own user_metadata through Supabase's API: fine for a demo,
 * not fine as the source of truth for who may read whose orders. The `app_users` table in
 * supabase/schema.sql is the version to copy.
 */
/*
 * Ask the gateway who an address belongs to. Server-to-server, with the same key /v1/session
 * takes.
 *
 * This is why this application needs no MongoDB driver and no database credential of its own:
 * `users.email` is a field the gateway's field policy denies to everything else, and the gateway
 * is the one place allowed to read it.
 */
async function lookupIdentity(email) {
  if (!WIDGET_API_KEY) return null;
  try {
    const response = await fetch(`${GATEWAY_URL}/v1/identity/lookup`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${WIDGET_API_KEY}`,
      },
      body: JSON.stringify({ email }),
    });
    if (!response.ok) {
      console.error("[host] identity lookup failed:", response.status);
      return null;
    }
    return await response.json();
  } catch (error) {
    console.error("[host] identity lookup failed:", error);
    return null;
  }
}

async function fetchScope(user) {
  if (SUPABASE_SERVICE_ROLE_KEY) {
    const url = `${SUPABASE_URL}/rest/v1/app_users?id=eq.${encodeURIComponent(user.id)}&select=vendor_id,display_name`;
    const response = await fetch(url, {
      headers: {
        apikey: SUPABASE_SERVICE_ROLE_KEY,
        Authorization: `Bearer ${SUPABASE_SERVICE_ROLE_KEY}`,
      },
    });
    if (response.ok) {
      const rows = await response.json();
      if (Array.isArray(rows) && rows.length) {
        return {
          userId: rows[0].app_user_id || null,
          role: rows[0].role || null,
          displayName: rows[0].display_name || null,
        };
      }
    }
  }
  // Fall back to asking the gateway who this email is -- which keeps the Supabase path and the
  // OTP path resolving roles from the same source of truth (`users.usertype`) instead of two
  // that can disagree.
  const identity = await lookupIdentity(String(user.email || "").toLowerCase());
  if (identity && identity.found) {
    return { userId: identity.user_id, role: identity.role, displayName: identity.name };
  }
  return { userId: null, role: null, displayName: null };
}

/* ---- helpers ----------------------------------------------------------------------------- */

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml",
};

function json(response, statusCode, body, headers = {}) {
  const payload = JSON.stringify(body);
  response.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    ...headers,
  });
  response.end(payload);
}

async function readJsonBody(request, limitBytes = 64 * 1024) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > limitBytes) throw new Error("body_too_large");
    chunks.push(chunk);
  }
  if (!chunks.length) return {};
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

async function serveStatic(response, urlPath) {
  const relative = normalize(urlPath === "/" ? "/index.html" : urlPath).replace(/^(\.\.[/\\])+/, "");
  const filePath = join(PUBLIC_DIR, relative);
  if (!filePath.startsWith(PUBLIC_DIR)) {
    response.writeHead(403).end("Forbidden");
    return;
  }
  try {
    const body = await readFile(filePath);
    response.writeHead(200, {
      "Content-Type": MIME[extname(filePath)] || "application/octet-stream",
    });
    response.end(body);
  } catch {
    response.writeHead(404, { "Content-Type": "text/plain" }).end("Not found");
  }
}

/* ---- routes ------------------------------------------------------------------------------ */

const routes = {
  /* What the browser is allowed to know: the public Supabase values, and where the gateway is.
   * Served as a script rather than baked into index.html so the same static files work against
   * any environment without a build step. */
  "GET /config.js": async (_request, response) => {
    const config = {
      supabaseUrl: SUPABASE_URL,
      supabaseAnonKey: SUPABASE_ANON_KEY,
      gatewayUrl: GATEWAY_URL,
      demoMode: DEMO_MODE,
      otpProvider: otpConfig.provider,
      supabaseEnabled: Boolean(SUPABASE_URL && SUPABASE_ANON_KEY),
    };
    response.writeHead(200, { "Content-Type": MIME[".js"], "Cache-Control": "no-store" });
    response.end(`window.HOST_CONFIG = ${JSON.stringify(config)};`);
  },

  /* Exchange a Supabase access token for this application's own session cookie. Done once, at
   * sign-in, so the access token itself never has to be replayed on later requests. */
  "POST /api/auth/session": async (request, response) => {
    if (!SUPABASE_URL || !SUPABASE_ANON_KEY) {
      return json(response, 503, { error: "Supabase is not configured on this server." });
    }
    const body = await readJsonBody(request);
    if (!body.access_token) return json(response, 400, { error: "Missing access_token." });

    const user = await fetchSupabaseUser(body.access_token);
    if (!user || !user.id) return json(response, 401, { error: "That sign-in isn't valid." });

    const { userId, role, displayName } = await fetchScope(user);
    if (!role) {
      return json(response, 403, { error: "That account isn't mapped to a role yet." });
    }
    const session = {
      userId,
      email: user.email || null,
      role,
      displayName: displayName || user.email || null,
    };
    return json(response, 200, { user: publicUser(session) }, { "Set-Cookie": makeSessionCookie(session) });
  },

  /*
   * Step one of signing in: the user gives an address, and gets a code if it belongs to someone.
   *
   * The reply is identical whether or not it does. That is not politeness -- an endpoint that
   * says "no such account" is a customer-list oracle that anyone can query, and this application
   * is fronting a database of real customers and vendors.
   */
  "POST /api/auth/request-code": async (request, response) => {
    const body = await readJsonBody(request);
    const email = String(body.email || "").trim().toLowerCase();
    if (!email || !email.includes("@")) {
      return json(response, 400, { error: "Enter an email address." });
    }

    // The gateway owns the `users` collection -- and `email` is a field its field policy denies to
    // everything else -- so "does this address belong to anyone, and what are they" is a
    // server-to-server question, not a Mongo credential this app has to hold.
    const identity = await lookupIdentity(email);
    const result = await requestCode(email, { exists: Boolean(identity && identity.found) });

    if (result.throttled) {
      // The one thing worth distinguishing: it tells a real user to wait rather than to keep
      // trying, and it says nothing about whether the address exists.
      return json(response, 429, {
        error: "Too many codes requested for that address. Try again later.",
      });
    }
    return json(response, 200, {
      sent: true,
      // Console mode only, so the demo can be completed without an email provider. Never
      // populated once a real provider is configured.
      devCode: result.code || null,
    });
  },

  /* Step two: the code comes back, and a session is issued if it matches. */
  "POST /api/auth/verify-code": async (request, response) => {
    const body = await readJsonBody(request);
    const email = String(body.email || "").trim().toLowerCase();
    const verdict = verifyCode(email, body.code);

    if (!verdict.ok) {
      // One message for every failure -- wrong, expired, never issued, or out of attempts. The
      // differences are useful to an attacker (which addresses have live codes) and not to a
      // user, who retries or asks for a new code either way.
      console.warn(`[auth] code rejected (${verdict.reason})`);
      return json(response, 401, { error: "That code isn't right, or it has expired." });
    }

    // Re-resolved *after* verification rather than trusted from step one: the account may have
    // been suspended in between, and this is the lookup whose answer becomes a role.
    const identity = await lookupIdentity(email);
    if (!identity || !identity.found) {
      return json(response, 403, { error: "That account can't sign in right now." });
    }

    const session = {
      userId: identity.user_id,
      email,
      role: identity.role,
      displayName: identity.name || email,
    };
    return json(
      response,
      200,
      { user: publicUser(session) },
      { "Set-Cookie": makeSessionCookie(session) }
    );
  },

  /* Demo sign-in, only when DEMO_MODE=true. Exists so this example runs end to end before a
   * Supabase project does -- it asserts an identity rather than proving one, exactly like
   * app/slack/auth.py's `/login`, and for the same reason: showing the scoping guardrail work
   * shouldn't require standing up an identity provider first. */
  "POST /api/auth/demo": async (request, response) => {
    if (!DEMO_MODE) return json(response, 404, { error: "Not found." });
    const body = await readJsonBody(request);
    const role = ["admin", "vendor", "customer"].includes(body.role) ? body.role : "customer";
    const session = {
      userId: body.user_id || `demo-${role}`,
      email: `${body.user_id || role}@demo.local`,
      role,
      displayName: body.user_id ? `${role} ${body.user_id}` : `Demo ${role}`,
    };
    return json(response, 200, { user: publicUser(session) }, { "Set-Cookie": makeSessionCookie(session) });
  },

  "POST /api/auth/logout": async (_request, response) =>
    json(
      response,
      200,
      { ok: true },
      { "Set-Cookie": `${COOKIE_NAME}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0` }
    ),

  "GET /api/me": async (request, response) => {
    const session = readSession(request);
    if (!session) return json(response, 401, { error: "Not signed in." });
    return json(response, 200, { user: publicUser(session) });
  },

  /*
   * The integration point. Everything else in this file is ordinary web-app plumbing; this is the
   * thirty lines a real host application has to write.
   *
   * Note what is *not* read here: the request body. The widget sends nothing, and if it did, it
   * would be ignored -- the principal and the vendor scope both come out of the session cookie
   * this server signed, so the browser has no input into either.
   */
  "POST /api/chat-token": async (request, response) => {
    const session = readSession(request);
    if (!session) return json(response, 401, { error: "Not signed in." });
    if (!WIDGET_API_KEY) {
      return json(response, 503, { error: "The chat gateway key is not configured." });
    }

    const gatewayResponse = await fetch(`${GATEWAY_URL}/v1/session`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${WIDGET_API_KEY}`,
      },
      // `name` is what the widget greets this person with. Optional, and it comes from here for
      // the same reason the role does: this server knows who they are, and the gateway holds an
      // id. Sending nothing simply produces a greeting without a name.
      body: JSON.stringify({
        user_id: session.userId,
        role: session.role,
        name: session.displayName || null,
      }),
    });

    if (!gatewayResponse.ok) {
      console.error("[host] gateway refused a session request:", gatewayResponse.status);
      return json(response, 502, { error: "The assistant is unavailable right now." });
    }
    // Passed straight through: {token, expires_in}. This server does not store it -- the browser
    // holds it in memory for its lifetime and asks again when it expires.
    return json(response, 200, await gatewayResponse.json());
  },
};

function publicUser(session) {
  return {
    email: session.email,
    displayName: session.displayName,
    userId: session.userId,
    role: session.role,
    scoped: session.role !== "admin",
  };
}

/* ---- server ------------------------------------------------------------------------------ */

const server = createServer(async (request, response) => {
  const url = new URL(request.url, `http://${request.headers.host || "localhost"}`);
  const handler = routes[`${request.method} ${url.pathname}`];

  try {
    if (handler) return await handler(request, response);
    if (request.method === "GET") return await serveStatic(response, url.pathname);
    return json(response, 405, { error: "Method not allowed." });
  } catch (error) {
    // The message stays generic for the same reason app/messages.py exists: an error reply is an
    // output channel, and this one would otherwise hand a stack trace to a browser.
    console.error(`[host] ${request.method} ${url.pathname} failed:`, error);
    return json(response, 500, { error: "Something went wrong. Try again." });
  }
});

// Marker used by every secret placeholder in .env.example, so the sample can show the shape of a
// credential without that value ever working. A sample that ships a usable cookie-signing key is
// worse than one that ships none: every copy shares a secret printed in the repository.
const PLACEHOLDER_MARKER = "change_me";
const isPlaceholder = (value) => value.toLowerCase().includes(PLACEHOLDER_MARKER);

function checkConfig() {
  const problems = [];
  if (!SESSION_SECRET || SESSION_SECRET.length < 32) {
    problems.push(
      "SESSION_SECRET is missing or shorter than 32 characters. Session cookies are signed with " +
        "it; without one, anyone can forge a session. Generate one with: " +
        "node -e \"console.log(require('crypto').randomBytes(32).toString('base64url'))\""
    );
  } else if (isPlaceholder(SESSION_SECRET)) {
    problems.push(
      "SESSION_SECRET is still the placeholder from .env.example. Generate a real one with: " +
        "node -e \"console.log(require('crypto').randomBytes(32).toString('base64url'))\""
    );
  }
  if (!WIDGET_API_KEY) {
    problems.push("WIDGET_API_KEY is not set, so the chat widget cannot get a session token.");
  } else if (isPlaceholder(WIDGET_API_KEY)) {
    problems.push(
      "WIDGET_API_KEY is still the placeholder from .env.example. It must match one of the " +
        "secrets in the gateway's WIDGET_API_KEYS."
    );
  }
  if (otpConfig.provider !== "console" && !process.env.OTP_EMAIL_API_KEY) {
    problems.push(
      `OTP_EMAIL_PROVIDER=${otpConfig.provider} but OTP_EMAIL_API_KEY is not set, so no sign-in ` +
        "code can be delivered. Set the key, or use OTP_EMAIL_PROVIDER=console to print codes " +
        "to this log instead."
    );
  }
  return problems;
}

const problems = checkConfig();
if (problems.length) {
  console.error("\nCannot start:\n" + problems.map((p) => `  - ${p}`).join("\n") + "\n");
  process.exit(1);
}

if (DEMO_MODE) {
  console.warn(
    "\n  DEMO_MODE is on. Anyone can sign in as any vendor without proving anything.\n" +
      "  Never run this way anywhere real.\n"
  );
}

server.listen(PORT, "127.0.0.1", () => {
  console.log(`Host app:  http://127.0.0.1:${PORT}`);
  console.log(`Gateway:   ${GATEWAY_URL}`);
  console.log(
    `Auth:      email one-time code (${otpConfig.provider})` +
      `${SUPABASE_URL ? " + Supabase" : ""}${DEMO_MODE ? " + demo buttons" : ""}`
  );
  console.log(`Boot id:   ${randomUUID().slice(0, 8)}`);
});
