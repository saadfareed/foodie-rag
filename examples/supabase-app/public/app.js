/*
 * Host-page script. Two jobs: sign the user in, and load the chat plugin once there is a session
 * for it to run under.
 *
 * Sign-in is a one-time code by default: an address goes to this app's own server, a code comes
 * back by email, and the verified code is exchanged for an httpOnly session cookie. Nothing on
 * this page ever holds a credential the chat gateway would accept, and nothing on this page
 * decides what role the user has -- that comes from the `users` collection, server-side.
 *
 * Note what is deliberately absent from every response handled here: any signal about whether an
 * address is registered. "Check your email" is the answer either way, because an endpoint that
 * says "no such account" is a customer-list oracle anyone can query.
 */

const CONFIG = window.HOST_CONFIG || {};

const el = (id) => document.getElementById(id);
const show = (node, visible) => node.toggleAttribute("hidden", !visible);

let supabase = null;
let widgetLoaded = false;

/* ---- Supabase (loaded from CDN only when configured) ------------------------------------- */

async function getSupabase() {
  if (supabase) return supabase;
  if (!CONFIG.supabaseUrl || !CONFIG.supabaseAnonKey) return null;
  const { createClient } = await import("https://esm.sh/@supabase/supabase-js@2");
  supabase = createClient(CONFIG.supabaseUrl, CONFIG.supabaseAnonKey);
  return supabase;
}

/* ---- session ----------------------------------------------------------------------------- */

async function currentUser() {
  const response = await fetch("/api/me", { credentials: "include" });
  return response.ok ? (await response.json()).user : null;
}

async function signInWithSupabase(email, password) {
  const client = await getSupabase();
  if (!client) throw new Error("Supabase isn't configured on this server.");

  const { data, error } = await client.auth.signInWithPassword({ email, password });
  if (error) throw new Error(error.message);

  // The access token goes to our own server once, and is exchanged for a session cookie.
  const response = await fetch("/api/auth/session", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ access_token: data.session.access_token }),
  });
  if (!response.ok) throw new Error((await response.json()).error || "Sign-in failed.");
  return (await response.json()).user;
}

async function requestCode(email) {
  const response = await fetch("/api/auth/request-code", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "Couldn't send a code just now.");
  return data;
}

async function verifyCode(email, code) {
  const response = await fetch("/api/auth/verify-code", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, code }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "That code isn't right.");
  return data.user;
}

async function signInAsDemoRole(role, userId) {
  const response = await fetch("/api/auth/demo", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ role, user_id: userId || null }),
  });
  if (!response.ok) throw new Error((await response.json()).error || "Sign-in failed.");
  return (await response.json()).user;
}

async function signOut() {
  const client = await getSupabase();
  if (client) await client.auth.signOut();
  await fetch("/api/auth/logout", { method: "POST", credentials: "include" });
  // A full reload rather than tearing the widget down by hand: the widget holds a chat token in a
  // closure, and the only way to be sure it's gone is for the page to be.
  window.location.reload();
}

/* ---- the plugin -------------------------------------------------------------------------- */

/*
 * One script tag, injected after sign-in so a signed-out visitor doesn't get a chat launcher that
 * can only tell them to sign in. `data-token-endpoint` is this app's own endpoint -- the widget
 * never talks to Supabase and never learns the gateway's key.
 */
function loadChatWidget() {
  if (widgetLoaded) return;
  widgetLoaded = true;

  const script = document.createElement("script");
  script.src = `${CONFIG.gatewayUrl}/widget.js`;
  script.async = true;
  script.dataset.gateway = CONFIG.gatewayUrl;
  script.dataset.tokenEndpoint = "/api/chat-token";
  script.dataset.title = "Ask your data";
  script.dataset.accent = "#4f46e5";
  script.onerror = () => {
    console.error(`[host] could not load the chat widget from ${CONFIG.gatewayUrl}. Is the gateway running?`);
  };
  document.body.appendChild(script);
}

/* ---- rendering --------------------------------------------------------------------------- */

const SCOPE_NOTES = {
  vendor:
    "You see your own orders, your own vendor record, and the customers who have ordered from " +
    "you — nobody else's. The filter is applied to the database query in code, so there is no " +
    "phrasing that gets around it.",
  customer:
    "You see your own orders and your own account, plus the vendor directory. Another " +
    "customer's orders return nothing, however the question is worded.",
  admin: "This account is unscoped and sees everything. Sign in as a vendor or customer to watch the scoping work.",
};

function renderSignedIn(user) {
  el("fact-email").textContent = user.email || user.displayName || "—";
  el("fact-role").textContent = user.role || "—";
  el("fact-scope").textContent = user.userId || "—";
  el("fact-gateway").textContent = CONFIG.gatewayUrl;
  el("scope-note").textContent = SCOPE_NOTES[user.role] || "";

  el("who-name").textContent = `${user.displayName || user.email || "Signed in"} (${user.role})`;
  show(el("who"), true);
  show(el("signin-card"), false);
  show(el("dashboard"), true);
  loadChatWidget();
}

function renderSignedOut() {
  show(el("who"), false);
  show(el("dashboard"), false);
  show(el("signin-card"), true);
  show(el("email-form"), true);
  show(el("code-form"), false);
  show(el("supabase-block"), Boolean(CONFIG.supabaseEnabled));
  show(el("demo-block"), Boolean(CONFIG.demoMode));
}

function showError(message) {
  const node = el("signin-error");
  node.textContent = message;
  show(node, true);
}

/* ---- wiring ------------------------------------------------------------------------------ */

let pendingEmail = "";

el("email-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  show(el("signin-error"), false);
  pendingEmail = el("email").value.trim().toLowerCase();
  try {
    const result = await requestCode(pendingEmail);
    el("code-sent-to").textContent = `We've sent a code to ${pendingEmail}. It expires shortly.`;
    // Console mode only: the server prints the code instead of emailing it, and hands it back so
    // the demo can be completed without an email provider. Never populated with a real provider.
    if (result.devCode) {
      el("dev-code").textContent = `Demo mode — your code is ${result.devCode}`;
      show(el("dev-code"), true);
    }
    show(el("email-form"), false);
    show(el("code-form"), true);
    el("code").focus();
  } catch (error) {
    showError(error.message);
  }
});

el("code-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  show(el("signin-error"), false);
  try {
    renderSignedIn(await verifyCode(pendingEmail, el("code").value.trim()));
  } catch (error) {
    showError(error.message);
  }
});

el("back-to-email").addEventListener("click", () => {
  show(el("code-form"), false);
  show(el("email-form"), true);
  show(el("dev-code"), false);
  show(el("signin-error"), false);
});

el("signin-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  show(el("signin-error"), false);
  try {
    renderSignedIn(await signInWithSupabase(el("sb-email").value, el("password").value));
  } catch (error) {
    showError(error.message);
  }
});

for (const button of document.querySelectorAll("[data-demo-role]")) {
  button.addEventListener("click", async () => {
    show(el("signin-error"), false);
    try {
      renderSignedIn(await signInAsDemoRole(button.dataset.demoRole, button.dataset.demoUser));
    } catch (error) {
      showError(error.message);
    }
  });
}

el("sign-out").addEventListener("click", signOut);

// window.DataChat is the widget's own handle (see app/api/static/widget.js) -- it opens the panel
// and submits the question, so a suggestion behaves exactly as if the user had typed it.
el("suggestions").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (button && window.DataChat) window.DataChat.ask(button.textContent.trim());
});

currentUser().then((user) => (user ? renderSignedIn(user) : renderSignedOut()));
