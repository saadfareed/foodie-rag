"""LangGraph state schema for the multi-domain agent graph (app/agents/graph.py)."""

from typing import Annotated, TypedDict

from app.agents.classifier import Classification
from app.rag.query_spec import QueryError, QuerySpec


def _merge_dicts(a: dict, b: dict) -> dict:
    return {**a, **b}


class GraphState(TypedDict, total=False):
    question: str
    user_id: str | None
    channel_id: str | None
    # The previous turn's resolved_question for this (channel, user), from
    # app/rag/conversation_context.py -- None for a fresh conversation or when the answer_cache/
    # clarification path already short-circuited. Read only by _classify_node, which decides
    # (via Classification.context_mode) whether the current question actually needs it.
    previous_question: str | None

    classification: Classification
    # Set once by _classify_node from classification.resolved_question -- either the question
    # verbatim (new_topic) or a context-folded rewrite (followup). Every downstream node
    # (_resolve_anchors_node, the domain_agent fan-out, _synthesize_node) generates/answers
    # against this instead of the raw `question`, via _effective_question() in graph.py.
    resolved_question: str

    # Present only inside a single fanned-out domain_agent invocation (see Send() in graph.py) --
    # never appears in the overall merged state.
    domain: str

    # Cross-domain anchors resolved once, ahead of the per-domain fan-out (see
    # _resolve_anchors_node in graph.py) -- plain fields, written by exactly one node.
    resolved_customer_location: dict
    resolved_customer_ids: list[str]
    resolved_vendor_ids: list[str]

    # Created once by _classify_node and shared (by reference, mutated in place -- not merged
    # via a reducer) by _resolve_anchors_node and every fanned-out domain_agent node, so a
    # (domain, question) pair generated once during anchor resolution isn't generated again via a
    # second, identical Gemini call during the fan-out (see _generate_validate_execute).
    spec_cache: dict[tuple[str, str], "QuerySpec | QueryError"]

    specs_by_domain: Annotated[dict[str, QuerySpec], _merge_dicts]
    rows_by_domain: Annotated[dict[str, list[dict]], _merge_dicts]
    # A domain agent's own deliberate "this question can't be answered from my schema" --
    # distinct from errors_by_domain (validation/execution failures) so synthesize_node can
    # surface it as a plain, friendly rejection rather than an "I ran into a problem" framing.
    out_of_scope_by_domain: Annotated[dict[str, str], _merge_dicts]
    errors_by_domain: Annotated[dict[str, str], _merge_dicts]
    timings: Annotated[dict[str, float], _merge_dicts]

    answer: str
    needs_clarification: bool
    # Set by _confirm_context_switch_node when classify decided this message doesn't fit the
    # still-live conversation (context_mode == "new_topic" while previous_question is set) --
    # `answer` is a yes/no prompt in that case, not a real answer. Mutually exclusive with
    # needs_clarification: _route_after_classify picks at most one branch per invocation.
    needs_context_confirmation: bool
