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

    classification: Classification

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
