"""LangGraph state machine: classify -> clarify | confirm a context switch | resolve cross-domain
anchors -> fan out to per-domain agents -> validate -> execute -> synthesize.

    question
       |
       v
    [classify] --confidence too low, no domain, or model asked for clarification--> [clarify] -> END
       |
       |--context_mode=="new_topic" AND a previous_question is still live-->
       |     [confirm_context_switch] -- asks the user to confirm before discarding that context;
       |                                 no LLM call, no query generated yet -> END
       v (confident, and either on-topic or nothing live to discard)
    [resolve_anchors] -- only does real work for "vendors near <customer>[, with pending
       |                  orders]": resolves the named customer's coordinates (and, if orders
       |                  is also in play, which vendors are nearby) *in code*, not by asking a
       |                  model to invent coordinates for a name/id it can't see (see
       |                  app/agents/query_agents.py's geo rule). A no-op for every other
       |                  question shape.
       v
    [domain_agent] x N (parallel, one per classified domain, capped at settings.agent_max_fan_out)
       |              each: schema-scoped LangChain chain -> QuerySpec -> scope_spec_to_domain()
       |              (forces collection + usertype) -> validate_query_spec() ->
       |              execute_query_spec()
       v
    [synthesize] -> END

This is intentionally *not* a generic multi-hop query planner: the cross-domain sequencing here
covers the one named pattern from the design (customer -> vendor geo anchor -> order filter),
implemented explicitly rather than as an open-ended dependency graph, because that's the actual
requirement today and a generic planner would be speculative machinery for cases that don't
exist yet.
"""

import logging
import time

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app.agents.classifier import Classification, classify_question
from app.agents.domains import DOMAINS, allowed_collections, geo_allowed_fields, merge_forced_filter
from app.agents.enrichment import enrich_rows_with_names
from app.agents.query_agents import generate_domain_query_spec
from app.agents.state import GraphState
from app.config import settings
from app.db.executor import execute_query_spec
from app.db.mongo import get_db
from app.llm.circuit_breaker import CircuitBreakerOpenError
from app.llm.gemini_client import GeminiClient, is_rate_limited
from app.rag.query_spec import GeoNear, QueryError, QuerySpec
from app.rag.validator import QueryValidationError, validate_query_spec

logger = logging.getLogger("audit")


def _friendly_generation_error(exc: Exception) -> str:
    """Mirrors the top-level framing in app/rag/pipeline.py for the same exception types -- a
    per-domain generation failure (mid-fan-out, after classification already ran) should read
    the same way a whole-question failure would, not leak internal exception text (e.g. "Gemini
    circuit breaker open after 5 consecutive failures -- failing fast instead of retrying.")
    just because it happened to surface here instead of escaping the whole graph."""
    if isinstance(exc, CircuitBreakerOpenError):
        return "Gemini is temporarily unavailable -- please try again shortly."
    if is_rate_limited(exc):
        return "Gemini rate limit reached -- please try again shortly."
    return str(exc)


_DEFAULT_CLARIFICATION = (
    "I'm not sure what data this question needs -- could you clarify whether it's about "
    "orders, customers, or vendors, and include a location if it's a 'nearby' question?"
)


def _generate_validate_execute(
    gemini: GeminiClient,
    domain_name: str,
    question: str,
    *,
    geo_override_location: dict | None = None,
    id_filter: dict | None = None,
    limit_override: int | None = None,
    spec_cache: dict[tuple[str, str], QuerySpec | QueryError] | None = None,
) -> tuple[QuerySpec | None, list[dict], str | None, bool]:
    """Shared by the real per-domain agent node and the anchor-resolution step below -- both
    need "generate a spec for this domain, force in whatever code already knows, validate,
    execute" and shouldn't drift into two slightly different implementations of the same
    guardrails.

    `spec_cache`, keyed by (domain_name, question), memoizes just the *generation* call (the
    Gemini round trip) for one graph.invoke(): the cross-domain "vendors near customer X with
    pending orders" pattern generates the identical (domain, question) spec twice with identical
    overrides -- once in _resolve_anchors_node to resolve coordinates/ids, once here in the real
    fan-out -- differing only in limit_override, which is applied *after* generation anyway. A
    cached hit still runs its own validate/execute (never cached) and deep-copies the spec before
    mutating it, so each call site's overrides can't leak into the other's.

    Returns (spec, rows, error, out_of_scope). out_of_scope distinguishes the domain agent's own
    deliberate "can't answer this" (QueryError) from an actual failure -- confirmed in
    production: the model can emit a QuerySpec shape Pydantic rejects (e.g. an explicit
    "limit": null that used to hard-fail validation), and a raw exception there is a bug/model
    hiccup, not a friendly rejection, so it must not be presented the same way."""
    domain = DOMAINS[domain_name]
    cache_key = (domain_name, question)
    if spec_cache is not None and cache_key in spec_cache:
        result = spec_cache[cache_key]
    else:
        try:
            result = generate_domain_query_spec(gemini, domain, question)
        except Exception as exc:  # noqa: BLE001 -- a generation-time failure, not this domain's fault
            return None, [], _friendly_generation_error(exc), False
        if spec_cache is not None:
            spec_cache[cache_key] = result

    if isinstance(result, QueryError):
        return None, [], result.error, True

    spec = result.model_copy(deep=True)
    if geo_override_location is not None and domain.geo_field is not None:
        radius = spec.requested_radius_m or settings.mongodb_max_geo_radius_m
        spec.geo_near = GeoNear(
            field=domain.geo_field,
            longitude=geo_override_location["longitude"],
            latitude=geo_override_location["latitude"],
            max_distance_m=radius,
        )
    if id_filter:
        merge_forced_filter(spec, id_filter)
    if limit_override is not None:
        spec.limit = min(spec.limit, limit_override)

    try:
        validated = validate_query_spec(
            spec,
            allowed_collections=allowed_collections(),
            geo_allowed_fields=geo_allowed_fields(),
            max_geo_radius_m=settings.mongodb_max_geo_radius_m,
        )
    except QueryValidationError as exc:
        return spec, [], str(exc), False

    try:
        rows = execute_query_spec(get_db(), validated, timeout_ms=settings.mongodb_query_timeout_ms)
    except Exception as exc:  # noqa: BLE001 -- surfaced to the user as a domain-scoped error
        return validated, [], str(exc), False

    return validated, rows, None, False


def _classify_node(gemini: GeminiClient):
    def node(state: GraphState) -> dict:
        start = time.perf_counter()
        classification = classify_question(
            gemini, state["question"], previous_question=state.get("previous_question")
        )
        elapsed = round((time.perf_counter() - start) * 1000, 2)
        # Created once here (the first node every invocation passes through) so it exists in
        # state by the time _resolve_anchors_node and the fanned-out domain_agent nodes run --
        # see _generate_validate_execute's spec_cache parameter above. resolved_question is set
        # here too (see _effective_question below) so every downstream node generates/answers
        # against the context-folded question rather than the raw fragment when this turn was a
        # follow-up.
        return {
            "classification": classification,
            "resolved_question": classification.resolved_question,
            "spec_cache": {},
            "timings": {"classify_ms": elapsed},
        }

    return node


def _effective_question(state: GraphState) -> str:
    """The question every node past classify should actually generate/answer against --
    resolved_question when classify has run (always set by _classify_node), the raw question as
    a defensive fallback otherwise."""
    return state.get("resolved_question") or state["question"]


def _route_after_classify(state: GraphState) -> str:
    c = state["classification"]
    if not c.domains or c.confidence < settings.agent_classifier_min_confidence:
        return "clarify"
    if c.clarification_question:
        return "clarify"
    # A previous turn's context is still live (state["previous_question"], from
    # app/rag/conversation_context.py) but classify decided *this* message doesn't need it --
    # rather than silently discarding that context, ask before answering. Only reachable when
    # previous_question is actually set: with nothing live to discard, a "new_topic" question is
    # just an ordinary question and goes straight to resolve_anchors as before.
    if c.context_mode == "new_topic" and state.get("previous_question"):
        return "confirm_context_switch"
    return "resolve_anchors"


def _clarify_node(state: GraphState) -> dict:
    c = state["classification"]
    return {
        "answer": c.clarification_question or _DEFAULT_CLARIFICATION,
        "needs_clarification": True,
    }


def _confirm_context_switch_node(state: GraphState) -> dict:
    previous = state.get("previous_question", "")
    return {
        "answer": (
            f'That looks unrelated to what we were just discussing ("{previous}") -- should I '
            "clear that context and answer this as a new question? Reply yes or no."
        ),
        "needs_context_confirmation": True,
    }


def _resolve_anchors_node(gemini: GeminiClient):
    def node(state: GraphState) -> dict:
        classification: Classification = state["classification"]
        domains = set(classification.domains)
        if not (classification.needs_geo and "vendors" in domains):
            return {}

        question = _effective_question(state)
        spec_cache = state.get("spec_cache")
        timings: dict[str, float] = {}
        update: dict = {}

        start = time.perf_counter()
        customer_location: dict | None = None
        if "customers" in domains:
            _, rows, _, _ = _generate_validate_execute(
                gemini, "customers", question, limit_override=5, spec_cache=spec_cache
            )
            if rows:
                coords = (rows[0].get("location") or {}).get("coordinates")
                if coords and len(coords) == 2:
                    customer_location = {"longitude": coords[0], "latitude": coords[1]}
                    update["resolved_customer_location"] = customer_location
                    ids = [r["user_id"] for r in rows if r.get("user_id")]
                    if ids:
                        update["resolved_customer_ids"] = ids
        timings["resolve_customer_anchor_ms"] = round((time.perf_counter() - start) * 1000, 2)

        if customer_location and "orders" in domains:
            start = time.perf_counter()
            _, vendor_rows, _, _ = _generate_validate_execute(
                gemini,
                "vendors",
                question,
                geo_override_location=customer_location,
                limit_override=20,
                spec_cache=spec_cache,
            )
            ids = [r["user_id"] for r in vendor_rows if r.get("user_id")]
            if ids:
                update["resolved_vendor_ids"] = ids
            timings["resolve_vendor_anchor_ms"] = round((time.perf_counter() - start) * 1000, 2)

        update["timings"] = timings
        return update

    return node


def _fan_out(state: GraphState) -> list[Send]:
    classification = state["classification"]
    domains = classification.domains[: settings.agent_max_fan_out]
    sends = []
    for domain_name in domains:
        payload: dict = {
            "question": _effective_question(state),
            "domain": domain_name,
            # Threaded explicitly because a Send payload is a fresh dict, NOT the graph state:
            # a fanned-out node sees only what is put here. Omitting this silently disabled the
            # whole `/login` guardrail -- _orders_id_filter/_vendors_id_filter read it from
            # their node's state, found nothing, and forced no vendor scoping at all, so an
            # authenticated vendor saw every vendor's rows. Nothing failed; the answers were
            # just wrong.
            "authenticated_vendor_id": state.get("authenticated_vendor_id"),
            # Shared with _resolve_anchors_node so a domain already queried while resolving a
            # cross-domain anchor (customers/vendors, in the geo composite pattern) doesn't pay
            # for an identical Gemini generation call a second time here.
            "spec_cache": state.get("spec_cache"),
        }
        if domain_name == "vendors" and "resolved_customer_location" in state:
            payload["resolved_customer_location"] = state["resolved_customer_location"]
        if domain_name == "orders":
            if "resolved_vendor_ids" in state:
                payload["resolved_vendor_ids"] = state["resolved_vendor_ids"]
            if "resolved_customer_ids" in state:
                payload["resolved_customer_ids"] = state["resolved_customer_ids"]
        sends.append(Send("domain_agent", payload))
    return sends


def _orders_id_filter(state: GraphState) -> dict | None:
    forced: dict = {}

    # RBAC: Restrict vendor to their own orders
    auth_vendor = state.get("authenticated_vendor_id")
    if auth_vendor:
        forced["vendor_id"] = auth_vendor
    elif state.get("resolved_vendor_ids"):
        forced["vendor_id"] = {"$in": state["resolved_vendor_ids"]}

    if state.get("resolved_customer_ids"):
        forced["customer_id"] = {"$in": state["resolved_customer_ids"]}
    return forced or None


def _vendors_id_filter(state: GraphState) -> dict | None:
    forced: dict = {}
    auth_vendor = state.get("authenticated_vendor_id")
    if auth_vendor:
        forced["user_id"] = auth_vendor
    return forced or None


def _domain_agent_node(gemini: GeminiClient):
    def node(state: GraphState) -> dict:
        domain_name = state["domain"]
        question = state["question"]
        start = time.perf_counter()

        geo_override_location = (
            state.get("resolved_customer_location") if domain_name == "vendors" else None
        )

        id_filter = None
        if domain_name == "orders":
            id_filter = _orders_id_filter(state)
        elif domain_name == "vendors":
            id_filter = _vendors_id_filter(state)

        spec, rows, error, out_of_scope = _generate_validate_execute(
            gemini,
            domain_name,
            question,
            geo_override_location=geo_override_location,
            id_filter=id_filter,
            spec_cache=state.get("spec_cache"),
        )

        if rows:
            # Resolve customer_id/vendor_id -> names so a report can show who an order is for
            # instead of an opaque id. A no-op (and no query) when the rows carry no such ids,
            # e.g. an aggregation grouped by status. Never fatal: the rows are already a correct
            # answer, and losing the whole question over a failed name lookup would be a worse
            # outcome than a report that shows ids.
            try:
                rows = enrich_rows_with_names(
                    get_db(), rows, timeout_ms=settings.mongodb_query_timeout_ms
                )
            except Exception:  # noqa: BLE001 -- enrichment is presentation, not correctness
                logger.warning("name_enrichment_failed", extra={"event": {"domain": domain_name}})

        elapsed = round((time.perf_counter() - start) * 1000, 2)
        update: dict = {
            "rows_by_domain": {domain_name: rows},
            "timings": {f"{domain_name}_agent_ms": elapsed},
        }
        if spec is not None:
            update["specs_by_domain"] = {domain_name: spec}
        if error is not None:
            if out_of_scope:
                update["out_of_scope_by_domain"] = {domain_name: error}
            else:
                update["errors_by_domain"] = {domain_name: error}
        return update

    return node


def _synthesize_node(gemini: GeminiClient):
    def node(state: GraphState) -> dict:
        start = time.perf_counter()
        rows_by_domain = state.get("rows_by_domain", {})
        errors_by_domain = state.get("errors_by_domain", {})
        out_of_scope_by_domain = state.get("out_of_scope_by_domain", {})
        total_rows = sum(len(rows) for rows in rows_by_domain.values())

        if total_rows == 0:
            if errors_by_domain:
                domain, error = next(iter(errors_by_domain.items()))
                answer = f"I ran into a problem answering that ({domain}: {error})."
            elif out_of_scope_by_domain:
                if len(out_of_scope_by_domain) == 1:
                    answer = next(iter(out_of_scope_by_domain.values()))
                else:
                    answer = "; ".join(
                        f"{domain}: {reason}" for domain, reason in out_of_scope_by_domain.items()
                    )
            else:
                answer = "I didn't find any data matching that question."
        else:
            skipped = {**out_of_scope_by_domain, **errors_by_domain}
            answer = gemini.generate_answer(
                _effective_question(state), rows_by_domain, skipped or None
            )

        elapsed = round((time.perf_counter() - start) * 1000, 2)
        return {"answer": answer, "timings": {"synthesize_ms": elapsed}}

    return node


def build_graph(gemini: GeminiClient):
    graph = StateGraph(GraphState)
    graph.add_node("classify", _classify_node(gemini))
    graph.add_node("clarify", _clarify_node)
    graph.add_node("confirm_context_switch", _confirm_context_switch_node)
    graph.add_node("resolve_anchors", _resolve_anchors_node(gemini))
    graph.add_node("domain_agent", _domain_agent_node(gemini))
    graph.add_node("synthesize", _synthesize_node(gemini))

    graph.add_edge(START, "classify")
    graph.add_conditional_edges(
        "classify",
        _route_after_classify,
        ["clarify", "confirm_context_switch", "resolve_anchors"],
    )
    graph.add_edge("clarify", END)
    graph.add_edge("confirm_context_switch", END)
    graph.add_conditional_edges("resolve_anchors", _fan_out, ["domain_agent"])
    graph.add_edge("domain_agent", "synthesize")
    graph.add_edge("synthesize", END)

    return graph.compile()
