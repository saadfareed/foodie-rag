"""Classifies a question into one or more domains (app/agents/domains.py) before any query is
generated.

Enum-constrained, structured output (Pydantic, not free text): the model can only ever name a
domain from DOMAIN_NAMES, and anything else fails Pydantic validation rather than being silently
accepted. This is intentionally a *cheap, narrow* call -- it never sees per-domain schema detail,
only domain names -- so it stays fast and doesn't itself become a source of field-level
hallucination. The actual collection + usertype scoping is enforced separately, in code, by
app/agents/domains.py::scope_spec_to_domain(), regardless of what's classified here.
"""

from typing import Literal

from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import PromptTemplate
from pydantic import BaseModel, Field

from app.agents.domains import DOMAIN_NAMES
from app.llm.gemini_client import GeminiClient

DomainName = Literal["orders", "customers", "vendors"]


class Classification(BaseModel):
    domains: list[DomainName] = Field(default_factory=list)
    needs_geo: bool = False
    confidence: float = 0.0
    clarification_question: str | None = None


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
    "- needs_geo: true if the question asks for something near/nearby/within a distance.\n"
    "- confidence: your confidence (0-1) that the listed domains are correct and sufficient.\n"
    "- clarification_question: if the question is ambiguous, missing a location for a geo "
    "question, or doesn't clearly map to any domain, a short question to ask the user; "
    "otherwise null.\n\n"
    "Examples:\n"
    '- "how many cash orders this week?" -> domains: [orders] (not customers/vendors -- '
    "nothing here asks about people, only payments)\n"
    '- "active vendors in Karachi" -> domains: [vendors] (not customers -- the question names '
    "vendors specifically)\n"
    '- "vendors near customer USR-1 with pending orders" -> domains: [orders, customers, '
    "vendors] (all three named/connected explicitly)\n\n"
    "{format_instructions}\n\n"
    "Question: {question}"
)


def classify_question(gemini: GeminiClient, question: str) -> Classification:
    prompt = _PROMPT.format(
        domain_names=", ".join(DOMAIN_NAMES),
        format_instructions=_PARSER.get_format_instructions(),
        question=question,
    )
    return gemini.generate_structured(prompt, Classification)
