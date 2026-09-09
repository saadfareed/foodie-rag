/*
 * The embeddable chat plugin.
 *
 * One <script> tag on a host page, no build step, no dependencies. Everything renders inside a
 * shadow root so the host application's CSS and this widget's CSS cannot reach each other -- a
 * plugin that inherits `* { box-sizing: content-box }` from its host looks broken through no
 * fault of its own, and a plugin that leaks `button { ... }` into its host is worse.
 *
 * Four rules in here are load-bearing rather than stylistic:
 *
 * 1. **Every piece of server text is written with textContent, never innerHTML.** Answers are
 *    generated from database rows. Slack rendered them as text; a browser would happily render
 *    a stored value as markup, which turns one poisoned row into stored XSS on every host page
 *    embedding this. There is no HTML-rendering path in this file at all, so there is nothing to
 *    forget to escape.
 * 2. **The answer streams, and the `result` event is what is authoritative.** Tokens are for
 *    watching the answer appear; the final `result` carries the text that was cached, audited and
 *    scanned as finished prose, so the bubble is replaced with it at the end. Progress events can
 *    legitimately be dropped under backpressure -- the result never is.
 * 3. **The greeting is asked for, not assembled here.** What a person may ask about depends on
 *    their role (`app/security/roles.py`), and their name is on the session the host application
 *    minted. A greeting written in this file would be a second copy of the access rules, in the
 *    one place that cannot see them -- so it comes from `/v1/me`, and `data-greeting` overrides
 *    it for a host that wants its own words.
 * 4. **The session token lives in a closure variable and nowhere else.** Not localStorage, not
 *    sessionStorage, not a cookie. It carries the row-level scope the backend answers under, so
 *    an XSS in the *host* page should not leave a durable credential lying around for it. It
 *    dies with the tab, and it is re-fetched from the host's own endpoint when it expires.
 *
 * Install:
 *   <script src="https://your-gateway/widget.js"
 *           data-gateway="https://your-gateway"
 *           data-token-endpoint="/api/chat-token"></script>
 *
 * `data-token-endpoint` is an endpoint on the *host application*, same-origin, that answers with
 * {"token": "..."} for whoever is currently logged in there. That endpoint is where the host
 * decides who the user is and what they may see; this file never makes that decision and cannot.
 */
(function () {
  "use strict";

  var script = document.currentScript;

  function attr(name, fallback) {
    var value = script && script.getAttribute(name);
    return value === null || value === undefined || value === "" ? fallback : value;
  }

  var CONFIG = {
    gateway: (attr("data-gateway", "") || "").replace(/\/+$/, ""),
    tokenEndpoint: attr("data-token-endpoint", "/api/chat-token"),
    title: attr("data-title", "Ask your data"),
    // Empty unless the host page set one. The opening line is otherwise asked for from the
    // gateway (/v1/me), which knows the signed-in person's name and what their role may ask
    // about -- neither of which this script can know, and neither of which it should guess.
    greeting: attr("data-greeting", ""),
    accent: attr("data-accent", "#4f46e5"),
    startOpen: attr("data-open", "false") === "true",
  };

  var STYLES = `
    :host { all: initial; font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
    *, *::before, *::after { box-sizing: border-box; }

    .launcher {
      position: fixed; right: 24px; bottom: 24px; z-index: 2147483000;
      width: 56px; height: 56px; border-radius: 50%; border: 0; cursor: pointer;
      background: var(--accent); color: #fff; font-size: 24px; line-height: 1;
      box-shadow: 0 8px 24px rgba(15, 23, 42, 0.28);
      display: flex; align-items: center; justify-content: center;
      transition: transform 120ms ease;
    }
    .launcher:hover { transform: scale(1.05); }
    .launcher:focus-visible { outline: 3px solid var(--accent); outline-offset: 3px; }

    .panel {
      position: fixed; right: 24px; bottom: 96px; z-index: 2147483000;
      width: min(400px, calc(100vw - 32px)); height: min(600px, calc(100vh - 140px));
      display: none; flex-direction: column; overflow: hidden;
      background: var(--surface); color: var(--text);
      border: 1px solid var(--border); border-radius: 16px;
      box-shadow: 0 24px 60px rgba(15, 23, 42, 0.24);
    }
    .panel[data-open="true"] { display: flex; }

    header {
      display: flex; align-items: center; gap: 10px;
      padding: 14px 16px; background: var(--accent); color: #fff;
    }
    header h1 { margin: 0; font-size: 15px; font-weight: 600; flex: 1; }
    header button {
      border: 0; background: transparent; color: #fff; cursor: pointer;
      font-size: 20px; line-height: 1; padding: 4px 6px; border-radius: 6px;
    }
    header button:hover { background: rgba(255, 255, 255, 0.18); }

    .log { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 12px; }

    .msg { max-width: 88%; padding: 10px 13px; border-radius: 14px; font-size: 14px; line-height: 1.5; white-space: pre-wrap; overflow-wrap: anywhere; }
    .msg[data-from="user"] { align-self: flex-end; background: var(--accent); color: #fff; border-bottom-right-radius: 4px; }
    .msg[data-from="bot"] { align-self: flex-start; background: var(--bubble); color: var(--text); border-bottom-left-radius: 4px; }
    .msg[data-tone="error"] { background: var(--error-bg); color: var(--error-text); }

    /* flex + fit-content, not inline-flex: inside a pre-wrap bubble an inline button lands
       mid-sentence after the answer's last line instead of below it. */
    .download {
      display: flex; width: fit-content; align-items: center; gap: 6px; margin-top: 10px;
      padding: 7px 11px; border-radius: 9px; font-size: 13px; font-weight: 600;
      text-decoration: none; background: var(--surface); color: var(--accent);
      border: 1px solid var(--border);
    }
    .download:hover { border-color: var(--accent); }

    .typing { align-self: flex-start; display: flex; align-items: center; gap: 9px; padding: 10px 14px; background: var(--bubble); border-radius: 14px; }
    .typing .dots { display: flex; gap: 4px; }
    .typing .dots i { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); animation: blink 1.2s infinite; }
    .typing .dots i:nth-child(2) { animation-delay: 0.2s; }
    .typing .dots i:nth-child(3) { animation-delay: 0.4s; }
    .typing .stage { font-size: 12px; color: var(--muted); }
    @keyframes blink { 0%, 60%, 100% { opacity: 0.25; } 30% { opacity: 1; } }

    form { display: flex; gap: 8px; padding: 12px; border-top: 1px solid var(--border); background: var(--surface); }
    textarea {
      flex: 1; resize: none; font: inherit; font-size: 14px; padding: 9px 11px;
      border: 1px solid var(--border); border-radius: 10px; max-height: 96px;
      background: var(--input-bg); color: var(--text);
    }
    textarea:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
    form button {
      border: 0; border-radius: 10px; padding: 0 15px; cursor: pointer;
      background: var(--accent); color: #fff; font-weight: 600; font-size: 14px;
    }
    form button:disabled { opacity: 0.5; cursor: not-allowed; }

    .hint { padding: 0 12px 10px; font-size: 11px; color: var(--muted); text-align: center; }

    :host {
      --accent: ${CONFIG.accent};
      --surface: #ffffff; --bubble: #f1f5f9; --text: #0f172a;
      --muted: #64748b; --border: #e2e8f0; --input-bg: #ffffff;
      --error-bg: #fef2f2; --error-text: #991b1b;
    }
    @media (prefers-color-scheme: dark) {
      :host {
        --surface: #0f172a; --bubble: #1e293b; --text: #e2e8f0;
        --muted: #94a3b8; --border: #334155; --input-bg: #1e293b;
        --error-bg: #450a0a; --error-text: #fecaca;
      }
    }
  `;

  /* ---- session token -------------------------------------------------------------------- */

  var token = null;
  var tokenExpiresAt = 0;

  /*
   * The host application's endpoint is same-origin and called with credentials, so the host's own
   * login cookie decides who this is. `force` re-fetches after a 401, which is the normal way an
   * expired token is discovered -- the alternative, trusting the local clock, silently fails for
   * anyone whose machine drifts.
   */
  function getToken(force) {
    if (!force && token && Date.now() < tokenExpiresAt - 30000) {
      return Promise.resolve(token);
    }
    return fetch(CONFIG.tokenEndpoint, {
      method: "POST",
      credentials: "include",
      headers: { Accept: "application/json" },
    }).then(function (response) {
      if (!response.ok) {
        var error = new Error("token_request_failed");
        error.status = response.status;
        throw error;
      }
      return response.json().then(function (data) {
        token = data.token;
        tokenExpiresAt = Date.now() + (data.expires_in || 1800) * 1000;
        return token;
      });
    });
  }

  // The fallback opening line, shown only when the gateway can't be asked. Deliberately vaguer
  // than any of the role-specific ones -- promising a customer more than they can see produces a
  // refusal they can't explain.
  var DEFAULT_GREETING =
    "Ask a question about your data. You can also ask for the answer as a CSV, Excel or PDF file.";

  /*
   * Who the gateway says this session is, and how it wants to greet them. One retry with a fresh
   * token, exactly like the ask paths: an expired token is the ordinary way this fails.
   */
  function fetchProfile(isRetry) {
    return getToken(Boolean(isRetry))
      .then(function (bearer) {
        return fetch(CONFIG.gateway + "/v1/me", {
          headers: { Accept: "application/json", Authorization: "Bearer " + bearer },
        });
      })
      .then(function (response) {
        if (response.status === 401 && !isRetry) return fetchProfile(true);
        if (!response.ok) throw new Error("profile_request_failed");
        return response.json();
      });
  }

  /*
   * What each stage means to a person. The server sends short machine names; translating them
   * here keeps the wording a presentation concern, and an unknown stage falls back to the
   * generic wait rather than showing a raw identifier.
   */
  var STAGE_LABELS = {
    understanding: "Reading your question",
    locating: "Finding the location",
    querying: "Looking up your data",
    writing: "Writing the answer",
    report: "Building your file",
  };

  /*
   * Streams the answer over Server-Sent Events, falling back to the plain endpoint if streaming
   * isn't available. Written against fetch + a reader rather than EventSource because EventSource
   * cannot send an Authorization header and cannot POST -- it would mean putting the session
   * token in the URL, which is exactly where a bearer credential should never be.
   */
  function streamGateway(question, handlers, isRetry) {
    return getToken(Boolean(isRetry))
      .then(function (bearer) {
        return fetch(CONFIG.gateway + "/v1/ask/stream", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Accept: "text/event-stream",
            Authorization: "Bearer " + bearer,
          },
          body: JSON.stringify({ question: question }),
        });
      })
      .then(function (response) {
        // One retry with a fresh token, and only one -- see askGateway below.
        if (response.status === 401 && !isRetry) {
          return streamGateway(question, handlers, true);
        }
        if (!response.ok) {
          // A 4xx is a complete JSON body, not a stream -- an empty or oversized question, say.
          // Read it as the final answer so the user sees the reason rather than nothing.
          return response.json().then(handlers.onResult);
        }
        if (!response.body) {
          // No readable stream in this browser. The plain endpoint returns the same answer, just
          // all at once, so the feature degrades to the pre-streaming behaviour instead of
          // failing.
          return askGateway(question, false).then(function (result) {
            handlers.onResult(result.data || {});
          });
        }
        return readEventStream(response.body, handlers);
      });
  }

  function readEventStream(body, handlers) {
    var reader = body.getReader();
    var decoder = new TextDecoder();
    var buffer = "";

    function dispatch(block) {
      var kind = null;
      var data = null;
      block.split("\n").forEach(function (line) {
        if (line.indexOf("event: ") === 0) kind = line.slice(7);
        else if (line.indexOf("data: ") === 0) data = line.slice(6);
      });
      if (!kind || data === null) return;
      var payload;
      try {
        payload = JSON.parse(data);
      } catch (error) {
        return;
      }
      if (kind === "stage") handlers.onStage(payload.text, payload.detail);
      else if (kind === "token") handlers.onToken(payload.text);
      else if (kind === "result" || kind === "error") handlers.onResult(payload);
    }

    function pump() {
      return reader.read().then(function (chunk) {
        if (chunk.done) {
          return;
        }
        buffer += decoder.decode(chunk.value, { stream: true });
        // SSE frames are separated by a blank line; anything after the last one is a partial
        // frame still arriving, so it stays in the buffer.
        var frames = buffer.split("\n\n");
        buffer = frames.pop();
        frames.forEach(dispatch);
        return pump();
      });
    }

    return pump();
  }

  function askGateway(question, isRetry) {
    return getToken(Boolean(isRetry))
      .then(function (bearer) {
        return fetch(CONFIG.gateway + "/v1/ask", {
          method: "POST",
          headers: { "Content-Type": "application/json", Authorization: "Bearer " + bearer },
          body: JSON.stringify({ question: question }),
        });
      })
      .then(function (response) {
        // One retry with a fresh token, and only one: a token that is rejected twice is being
        // rejected for a reason a third attempt won't change, and a retry loop against an auth
        // endpoint is how a widget turns one expired session into a request storm.
        if (response.status === 401 && !isRetry) {
          return askGateway(question, true);
        }
        return response.json().then(function (data) {
          return { ok: response.ok, data: data };
        });
      });
  }

  /* ---- UI ------------------------------------------------------------------------------- */

  function build() {
    var host = document.createElement("div");
    host.setAttribute("data-data-chat", "");
    var root = host.attachShadow({ mode: "open" });

    var style = document.createElement("style");
    style.textContent = STYLES;

    var launcher = document.createElement("button");
    launcher.className = "launcher";
    launcher.type = "button";
    launcher.textContent = "💬";
    launcher.setAttribute("aria-label", "Open " + CONFIG.title);

    var panel = document.createElement("div");
    panel.className = "panel";
    panel.setAttribute("data-open", String(CONFIG.startOpen));
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", CONFIG.title);

    var header = document.createElement("header");
    var heading = document.createElement("h1");
    heading.textContent = CONFIG.title;
    var close = document.createElement("button");
    close.type = "button";
    close.textContent = "×";
    close.setAttribute("aria-label", "Close");
    header.append(heading, close);

    var log = document.createElement("div");
    log.className = "log";
    log.setAttribute("role", "log");
    log.setAttribute("aria-live", "polite");

    var form = document.createElement("form");
    var input = document.createElement("textarea");
    input.rows = 1;
    input.placeholder = "How many orders are pending?";
    input.setAttribute("aria-label", "Your question");
    input.maxLength = 2000;
    var send = document.createElement("button");
    send.type = "submit";
    send.textContent = "Send";
    form.append(input, send);

    var hint = document.createElement("div");
    hint.className = "hint";
    hint.textContent = 'Type "reset" to start a new topic.';

    panel.append(header, log, form, hint);
    root.append(style, launcher, panel);
    document.body.appendChild(host);

    /* -- message rendering -- */

    function scroll() {
      log.scrollTop = log.scrollHeight;
    }

    function addMessage(from, text, tone) {
      var bubble = document.createElement("div");
      bubble.className = "msg";
      bubble.setAttribute("data-from", from);
      if (tone) bubble.setAttribute("data-tone", tone);
      // textContent, always. See the note at the top of this file.
      bubble.textContent = text;
      log.appendChild(bubble);
      scroll();
      return bubble;
    }

    function addDownload(bubble, file) {
      var link = document.createElement("a");
      link.className = "download";
      // The gateway sets Content-Disposition: attachment, so the browser saves rather than
      // navigates. `download` is deliberately absent: it is ignored cross-origin anyway, and
      // relying on it would break the moment the gateway is on its own domain.
      link.href = CONFIG.gateway + file.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "⬇ Download " + String(file.type || "file").toUpperCase();
      bubble.appendChild(link);
      scroll();
    }

    function showTyping() {
      var indicator = document.createElement("div");
      indicator.className = "typing";
      var dots = document.createElement("span");
      dots.className = "dots";
      dots.append(
        document.createElement("i"),
        document.createElement("i"),
        document.createElement("i")
      );
      var label = document.createElement("span");
      label.className = "stage";
      indicator.append(dots, label);
      log.appendChild(indicator);
      scroll();
      // setStage names what the wait is for. An unknown stage clears the label rather than
      // showing a raw identifier from the wire.
      indicator.setStage = function (name, detail) {
        var text = STAGE_LABELS[name] || "";
        label.textContent = text && detail ? text + " (" + detail + ")" : text;
        scroll();
      };
      return indicator;
    }

    /* -- interactions -- */

    var busy = false;

    function submit(question) {
      if (busy || !question) return;
      busy = true;
      send.disabled = true;
      addMessage("user", question);
      var typing = showTyping();

      var bubble = null;
      var streamed = "";

      function ensureBubble() {
        if (!bubble) {
          typing.remove();
          bubble = addMessage("bot", "");
        }
        return bubble;
      }

      streamGateway(question, {
        onStage: function (name, detail) {
          if (typing.setStage) typing.setStage(name, detail);
        },
        onToken: function (text) {
          streamed += text;
          // textContent, always -- see the note at the top of this file. This is the one place
          // model output is written repeatedly, so it is the one most worth being explicit about.
          ensureBubble().textContent = streamed;
          scroll();
        },
        onResult: function (data) {
          typing.remove();
          var text = data.text || "I couldn't get an answer just now. Try again in a moment.";
          // Replaced rather than appended: `result` is the text that was scanned, cached and
          // audited, and progress events may have been dropped on a slow connection. What was
          // streamed is a preview of this, not a part of it.
          var target = bubble || addMessage("bot", "");
          target.textContent = text;
          if (data.error) target.setAttribute("data-tone", "error");
          if (data.file && data.file.url) addDownload(target, data.file);
          scroll();
        },
      }, false)
        .catch(function (error) {
          if (bubble) bubble.remove();
          typing.remove();
          // A failure to even obtain a token means the host page's session is gone -- which is a
          // different instruction from "the bot had a problem", so it gets a different message.
          addMessage(
            "bot",
            error && error.status === 401
              ? "You need to be signed in before you can ask questions here. Sign in on this page and try again."
              : "I couldn't reach the assistant just now. Check your connection and try again.",
            "error"
          );
        })
        .finally(function () {
          busy = false;
          send.disabled = false;
          input.focus();
        });
    }

    var greeted = false;

    function greet() {
      // A host that supplied its own wording gets it immediately and no request is made.
      if (CONFIG.greeting) return addMessage("bot", CONFIG.greeting);
      fetchProfile(false)
        .then(function (profile) {
          addMessage("bot", (profile && profile.greeting) || DEFAULT_GREETING);
        })
        .catch(function () {
          // Not signed in, or the gateway is unreachable. Either way the generic line is a
          // better opening than an error for something the user hasn't asked for yet -- if they
          // do ask, submit() reports the real problem.
          addMessage("bot", DEFAULT_GREETING);
        });
    }

    function open() {
      panel.setAttribute("data-open", "true");
      // `greeted`, not the log's emptiness: the greeting arrives asynchronously, and a second
      // open() in the meantime would queue a second one.
      if (!greeted && !log.childElementCount) {
        greeted = true;
        greet();
      }
      input.focus();
    }

    launcher.addEventListener("click", function () {
      if (panel.getAttribute("data-open") === "true") {
        panel.setAttribute("data-open", "false");
      } else {
        open();
      }
    });
    close.addEventListener("click", function () {
      panel.setAttribute("data-open", "false");
      launcher.focus();
    });

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var question = input.value.trim();
      input.value = "";
      submit(question);
    });

    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        form.requestSubmit();
      }
    });

    if (CONFIG.startOpen) open();

    // A small, deliberately narrow handle for host pages that want to drive the widget --
    // opening it from their own "Ask about this" button, or asking a question on the user's
    // behalf. It exposes no way to set a token or a scope: those come from the host's server.
    window.DataChat = {
      open: open,
      close: function () {
        panel.setAttribute("data-open", "false");
      },
      ask: function (question) {
        open();
        submit(String(question || "").trim());
      },
    };
  }

  if (!CONFIG.gateway) {
    console.error("[data-chat] Missing data-gateway attribute on the widget script tag.");
    return;
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", build);
  } else {
    build();
  }
})();
