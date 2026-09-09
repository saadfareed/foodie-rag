"""Classifies a question into one or more domains (app/agents/domains.py) before any query is
generated.

Enum-constrained, structured output (Pydantic, not free text): the model can only ever name a
domain from DOMAIN_NAMES, and anything else fails Pydantic validation rather than being silently
accepted. This is intentionally a *cheap, narrow* call -- it never sees per-domain schema detail,
only domain names -- so it stays fast and doesn't itself become a source of field-level
hallucination. The actual collection + usertype scoping is enforced separately, in code, by
app/agents/domains.py::scope_spec_to_domain(), regardless of what's classified here.

Also does double duty as the conversational-continuity decision: when the caller passes
`previous_question` (the last resolved question for this (channel, user), from
app/rag/conversation_context.py), this same call decides whether the current message is a
standalone `new_topic` or a `followup` that only makes sense in light of that previous question
-- and if it's a follow-up, rewrites it into one self-contained `resolved_question` that
downstream domain agents (app/agents/query_agents.py) generate against instead of the raw
fragment. Piggybacking this on the classify call means an unrelated question costs nothing extra
(no previous-question text is even echoed into a longer prompt beyond one line), and a follow-up
never accumulates more than one prior turn's worth of context.
"""

from typing import Literal

from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import PromptTemplate
from pydantic import BaseModel, Field

from app.agents.domains import DOMAIN_NAMES
from app.llm.gemini_client import GeminiClient

DomainName = Literal["orders", "customers", "vendors"]
ContextMode = Literal["new_topic", "followup"]
OutputFormat = Literal["text", "csv", "xlsx", "pdf"]


class Classification(BaseModel):
    domains: list[DomainName] = Field(default_factory=list)
    needs_geo: bool = False
    confidence: float = 0.0
    clarification_question: str | None = None
    # Which format the user asked their answer back in. Carried on this existing call rather
    # than a dedicated one: a separate "classify the intent" round trip cost a full Gemini call
    # (against a free-tier daily quota measured in tens) to return a single word, on the hot
    # path of every question. The classifier is already reading the question closely enough to
    # decide domains; deciding format at the same time is free.
    #
    # An explicit phrase ("as csv", "excel file") is matched deterministically before this call
    # runs and overrides whatever the model says -- see app/services/intent_router.py. This
    # field is for the implicit cases the regex can't catch, like "put together a report".
    output_format: OutputFormat = "text"
    # Title for a generated CSV/XLSX/PDF, in the user's own terms: "last 10 incomplete order
    # details" -> "Last 10 Incomplete Order Details". Carried on this call for the same reason
    # as output_format -- the classifier is already reading the question closely, so naming the
    # report costs nothing extra. Empty means "no opinion"; the caller falls back to
    # REPORT_TITLE. A generic report title ("Data Report") on a specific request is a small
    # thing that makes a document feel like it wasn't actually about what was asked.
    report_title: str = ""
    # Whether resolved_question needed the previous turn's question to make sense. Defaults to
    # "new_topic" so a Classification built without ever mentioning context (e.g. every existing
    # test, or a call site that never passes previous_question) reads as standalone.
    context_mode: ContextMode = "new_topic"
    # Self-contained rewrite of the question: identical to the question that was classified when
    # context_mode is "new_topic"; folds in whatever's needed from the previous question when
    # "followup". Left "" by default -- classify_question() backfills it to the original question
    # text when the model leaves it blank, so callers never see an empty string.
    resolved_question: str = ""


_PARSER = PydanticOutputParser(pydantic_object=Classification)

_PROMPT = PromptTemplate.from_template(
    "You classify a question about a company database into one or more domains: {domain_names}. "
    "`customers` and `vendors` are both people records distinguished by an internal field you "
    "never see directly -- classify by what the question is about, not by field names.\n\n"
    "Rules:\n"
    "- domains: ONLY the domains whose data is actually required to answer the question -- not "
    "every domain that could theoretically relate to it. Each domain you include costs a real, "
    "separate database query and API call, so including one the question doesn't need is a real "
    "cost, not a safe default. A question entirely about payments/amounts/order status needs "
    "only orders -- do not add customers or vendors just because orders happen to reference "
    "them. Only include multiple domains when the question itself explicitly connects them, e.g. "
    '"vendors near a customer with pending orders" needs all three because it names a '
    "customer, a vendor, and an order condition together.\n"
    "- Asking for orders together with the customer's or vendor's own details (their name, city, "
    "loyalty tier, category, rating) is still ONLY orders: those columns are attached to each "
    "order automatically. Adding customers or vendors for them buys a second query that answers "
    "nothing and a refusal, because order status is not in the people collection.\n"
    "- needs_geo: true if the question asks for something near/nearby/within a distance.\n"
    '- output_format: the format the user wants the answer in. "text" unless they asked for a '
    'file or a document. "csv" for a raw data dump or export, "xlsx" for a spreadsheet or '
    'workbook, "pdf" for a report, a document, or anything asking for charts/graphs/visuals. '
    'When someone asks for a "report" or a "breakdown with a chart" without naming a file type, '
    'that is "pdf". A plain question -- even one whose answer happens to be a list -- is '
    '"text".\n'
    "- report_title: a short Title Case heading for the generated file, restating what the user "
    "asked for in their own terms. Keep every qualifier that identifies the data -- a count, a "
    "time range, a status, a named entity -- because those are what distinguish this report "
    'from the next one. Drop the format word and the request phrasing ("give me", "I need"). '
    'Examples: "I need last 10 incomplete order details in csv" -> "Last 10 Incomplete Order '
    'Details"; "vendors in Lahore as a pdf" -> "Vendors In Lahore"; "export all cash orders '
    'from last week to excel" -> "Cash Orders From Last Week". Leave it "" when output_format '
    'is "text".\n'
    "- confidence: your confidence (0-1) that the listed domains are correct and sufficient.\n"
    "- clarification_question: if the question is ambiguous, missing a location for a geo "
    "question, or doesn't clearly map to any domain, a short question to ask the user; "
    "otherwise null.\n"
    '- context_mode: "followup" ONLY if a previous question is shown below AND the current '
    "question cannot stand on its own without it -- e.g. it has no subject of its own, or uses "
    'words like "that", "those", "instead", "what about", or asks for a different aggregate '
    '("total", "average", "how many of those") of the same thing just asked about. Otherwise '
    '"new_topic" -- this is the default, and is always correct when no previous question is '
    "shown, or when the current question already names its own subject.\n"
    '- resolved_question: if context_mode is "new_topic", copy the current question verbatim. '
    'If "followup", rewrite it into ONE standalone question that folds in only what\'s needed '
    "from the previous question (the same entity/filters/time range) -- do not add anything not "
    "implied by either question, and do not describe the rewrite, just state it.\n\n"
    "Examples:\n"
    '- "how many cash orders this week?" -> domains: [orders] (not customers/vendors -- '
    "nothing here asks about people, only payments)\n"
    '- "active vendors in Karachi" -> domains: [vendors] (not customers -- the question names '
    "vendors specifically)\n"
    '- "incomplete orders with the customer details in pdf" -> domains: [orders] (not customers '
    "-- the customer's name/city/tier ride along on each order automatically)\n"
    '- "vendors near customer USR-1 with pending orders" -> domains: [orders, customers, '
    "vendors] (all three named/connected explicitly)\n"
    '- Previous question: "how many orders did vendor USR-2 have last week?" Current question: '
    '"what about the total amount?" -> context_mode: followup, domains: [orders], '
    'resolved_question: "what is the total amount of orders vendor USR-2 had last week?"\n'
    '- Previous question: "how many orders did vendor USR-2 have last week?" Current question: '
    '"list active customers in Lahore" -> context_mode: new_topic (a fresh, unrelated question '
    '-- ignore the previous one), domains: [customers], resolved_question: "list active '
    'customers in Lahore"\n\n'
    "{previous_question_section}"
    "{format_instructions}\n\n"
    "Question: {question}"
)


def classify_question(
    gemini: GeminiClient, question: str, previous_question: str | None = None
) -> Classification:
    previous_question_section = (
        "Previous question in this conversation (may or may not be related): "
        f'"{previous_question}"\n\n'
        if previous_question
        else ""
    )
    prompt = _PROMPT.format(
        domain_names=", ".join(DOMAIN_NAMES),
        format_instructions=_PARSER.get_format_instructions(),
        previous_question_section=previous_question_section,
        question=question,
    )
    classification = gemini.generate_structured(prompt, Classification)
    if not classification.resolved_question:
        classification.resolved_question = question
    return classification
