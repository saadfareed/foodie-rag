"""Deterministic, zero-cost pre-checks on the raw question: requested output format, and
requests for data the bot must refuse outright.

Both jobs used to be one extra Gemini call per question. That call was the single worst thing on
the hot path: it spent a request from a free-tier daily quota measured in tens, to return one
word, and it called the transport without the tuned generation config -- so it ran with
"thinking" enabled and paid several seconds of reasoning latency to answer "CSV".

Neither job needs a model.

* **Format** is stated in the user's own words ("as a csv", "excel file", "pdf report"). A regex
  catches the explicit cases exactly, and the implicit ones ("put together a report") ride along
  on the classifier call that already has to read the question anyway -- see
  app/agents/classifier.py::Classification.output_format.
* **Refusal** is a policy decision, and policy belongs in code. Asking a model whether a request
  is adversarial means the guardrail itself can be argued with; a pattern list cannot be talked
  out of refusing. This complements rather than replaces the structural guardrails: even a
  request that slips past here still cannot read a restricted field, because
  app/rag/validator.py rejects the query and app/db/executor.py strips the value.

Matching is on word boundaries throughout -- a substring search for "cc" would refuse "how many
accounts?", and one for "pin" would refuse "shipping".
"""

import re

from app.agents.classifier import OutputFormat

# Ordered most specific first: "excel report" should resolve to xlsx, not pdf, so the
# spreadsheet patterns are tested before the document ones.
_FORMAT_PATTERNS: tuple[tuple[OutputFormat, re.Pattern[str]], ...] = (
    (
        "xlsx",
        re.compile(
            r"\b(?:xlsx?|excel|spreadsheet|workbook|google\s+sheets?)\b"
            r"|\bas\s+(?:an?\s+)?sheet\b",
            re.IGNORECASE,
        ),
    ),
    ("csv", re.compile(r"\bcsv\b|\bcomma[- ]separated\b|\bdata\s+dump\b", re.IGNORECASE)),
    (
        "pdf",
        re.compile(
            r"\bpdf\b|\b(?:pie|bar|line)\s+chart\b|\bchart(?:s)?\b|\bgraph(?:s)?\b"
            r"|\bvisuali[sz]ation\b|\binfographic\b",
            re.IGNORECASE,
        ),
    ),
)

# Requests for credentials or payment instruments. Refused before any query is generated: the
# answer is "no" regardless of what the database holds, so there is nothing to look up.
_RESTRICTED_DATA_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:credit|debit)\s+cards?\b", re.IGNORECASE),
    re.compile(r"\bcard\s+(?:numbers?|details?|data|info(?:rmation)?)\b", re.IGNORECASE),
    re.compile(r"\bcvv\b|\bcvc\b", re.IGNORECASE),
    re.compile(r"\bpasswords?\b|\bpasscodes?\b|\bcredentials?\b", re.IGNORECASE),
    re.compile(r"\bapi[_\s-]?keys?\b|\bsecret\s+keys?\b|\bauth\s+tokens?\b", re.IGNORECASE),
    re.compile(r"\bssn\b|\bsocial\s+security\s+numbers?\b", re.IGNORECASE),
    re.compile(r"\b(?:bank|routing|iban)\s+(?:account\s+)?numbers?\b", re.IGNORECASE),
    re.compile(r"\bprivate\s+keys?\b", re.IGNORECASE),
)

# Attempts to read the datastore's own structure rather than the business data in it. These leak
# the shape of the system and help an attacker aim; they also never answer a real business
# question, which is why refusing costs nothing.
_INTROSPECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:list|show|dump|give)\b.{0,30}\b(?:indexes|indices)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:mongo(?:db)?|database|db)\s+(?:indexes|indices|schema|internals)\b", re.IGNORECASE
    ),
    re.compile(r"\b_id\b|\bobjectid\b", re.IGNORECASE),
    re.compile(r"\bconnection\s+string\b|\bmongodb\+srv\b|\bmongodb_uri\b", re.IGNORECASE),
    re.compile(r"\b(?:system|admin)\.\w+\b", re.IGNORECASE),
    re.compile(r"\bshow\s+(?:me\s+)?(?:the\s+)?collections?\b", re.IGNORECASE),
    re.compile(r"\benv(?:ironment)?\s+(?:vars?|variables?)\b", re.IGNORECASE),
    re.compile(r"\byour\s+(?:system\s+)?prompt\b|\bignore\s+(?:all\s+)?previous\b", re.IGNORECASE),
)

RESTRICTED_DATA_REFUSAL = (
    "I can't share credentials or payment details -- that data is restricted regardless of who "
    "asks. I can help with orders, customers and vendors instead."
)
INTROSPECTION_REFUSAL = (
    "I can't share database internals like indexes, record ids, or schema details -- they're "
    "not useful for answering business questions. Ask me about orders, customers or vendors "
    "and I'll query the data itself."
)


def detect_explicit_format(question: str) -> OutputFormat | None:
    """The format the user named outright, or None if they didn't name one.

    A returned value takes precedence over the classifier's inference: if someone typed the word
    "csv", no model judgement should be able to hand them a PDF.
    """
    for output_format, pattern in _FORMAT_PATTERNS:
        if pattern.search(question):
            return output_format
    return None


def refusal_reason(question: str) -> str | None:
    """A user-facing refusal if this question must not be answered, else None."""
    for pattern in _RESTRICTED_DATA_PATTERNS:
        if pattern.search(question):
            return RESTRICTED_DATA_REFUSAL
    for pattern in _INTROSPECTION_PATTERNS:
        if pattern.search(question):
            return INTROSPECTION_REFUSAL
    return None
