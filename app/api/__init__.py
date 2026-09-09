"""HTTP gateway for the embeddable web chat plugin.

A second *adapter* onto the same pipeline `app/slack/handlers.py` drives -- not a second
implementation of it. Everything that decides what an answer contains (the agent graph, the
validator, the field policy, the caches, the message catalogue) lives below this package and is
unaware it exists.

What this layer owns is everything Slack used to provide and a browser doesn't:

* **Identity.** Slack tells the bot who is speaking. A browser will tell you whatever it is
  asked to. So the host application's *server* mints a short-lived session token
  (`app/api/tokens.py`) carrying the principal and the row-level scope, and the browser only
  ever holds that.
* **Delivery.** Slack took report bytes inline via `files_upload_v2`. A browser needs a URL, so
  generated files are parked in a bounded TTL store (`app/api/files.py`) and handed out once,
  to the principal they were generated for.
* **Conversation identity.** Slack supplies a channel id. Here it is derived from the token, so
  a client cannot borrow another user's clarification state, follow-up context, or answer cache
  by naming their conversation.
"""
