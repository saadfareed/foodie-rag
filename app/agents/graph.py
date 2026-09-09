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
from app.llm.gemini_client import GeminiClient
from app.messages import (
    Failure,
    classify_exception,
    failure_message,
    new_reference,
    no_data_message,
    not_authorized_message,
    out_of_scope_message,
    sign_in_required_message,
    too_many_related_records_message,
)
from app.rag.query_spec import GeoNear, QueryError, QuerySpec
from app.rag.stream import NULL_SINK
from app.rag.validator import QueryValidationError, validate_query_spec
from app.security.roles import (
    ANONYMOUS,
    Principal,
    Role,
    forced_filter,
    may_query,
    needs_authorized_customers,
)

logger = logging.getLogger("audit")


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
) -> tuple[QuerySpec | None, list[dict], str | None, bool, Failure | None]:
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

    Returns (spec, rows, error, out_of_scope, failure). `error` is the RAW text, for the audit
    log only; `failure` is the classified kind that decides what the user is told. Keeping those
    two separate is the point: the raw text has carried pymongo tracebacks and validator internals
    into Slack, and re-deriving the kind by string-matching that text downstream was worse -- a
    Gemini 429 caught during generation came out phrased as a database error.

    out_of_scope distinguishes the domain agent's own deliberate "can't answer this"
    (QueryError) from an actual failure -- confirmed in production: the model can emit a
    QuerySpec shape Pydantic rejects (e.g. an explicit "limit": null that used to hard-fail
    validation), and a raw exception there is a bug/model hiccup, not a friendly rejection, so
    it must not be presented the same way."""
    domain = DOMAINS[domain_name]
    cache_key = (domain_name, question)
    if spec_cache is not None and cache_key in spec_cache:
        result = spec_cache[cache_key]
    else:
        try:
            result = generate_domain_query_spec(gemini, domain, question)
        except Exception as exc:  # noqa: BLE001 -- a generation-time failure, not this domain's fault
            return None, [], str(exc), False, classify_exception(exc)
        if spec_cache is not None:
            spec_cache[cache_key] = result

    if isinstance(result, QueryError):
        return None, [], result.error, True, None

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
        return spec, [], str(exc), False, Failure.QUERY_REJECTED

    try:
        rows = execute_query_spec(get_db(), validated, timeout_ms=settings.mongodb_query_timeout_ms)
    except Exception as exc:  # noqa: BLE001 -- surfaced to the user as a domain-scoped error
        return validated, [], str(exc), False, classify_exception(exc)

    return validated, rows, None, False, None


def _classify_node(gemini: GeminiClient):
    def node(state: GraphState) -> dict:
        start = time.perf_counter()
        _sink(state).stage("understanding")
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


def _sink(state: GraphState):
    """The stream sink for this invocation, or the no-op one.

    Every node goes through this rather than `state["stream_sink"]` directly: the key is absent
    on every Slack request and on any node whose Send payload forgot it, and a KeyError on the
    answer path would be a spectacular way to fail at emitting a progress message.
    """
    return state.get("stream_sink") or NULL_SINK


def _principal(state: GraphState) -> Principal:
    """The identity this invocation is answered under.

    Defaults to ANONYMOUS -- which can read nothing -- rather than to an unrestricted principal.
    A node that reads this from a `Send` payload that forgot to include it therefore refuses,
    instead of quietly answering from every row. The previous shape defaulted the other way and
    that is exactly how the `/login` scoping was silently disabled once before.
    """
    return state.get("principal") or ANONYMOUS


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
    # Authorization is checked here, before anything is generated or queried: a question this
    # principal may not have answered at all should cost nothing. A *partial* refusal (one of two
    # domains) carries on and is reported per-domain by _resolve_anchors_node.
    if _principal(state).role is Role.ANONYMOUS or not _authorized_domains(state):
        return "deny"
    return "resolve_anchors"


def _deny_node(state: GraphState) -> dict:
    """Nothing this principal may read was asked for.

    Two different messages because the fix is different: an anonymous caller has to sign in,
    while a signed-in one is asking for someone else's data and no amount of retrying will help.
    Neither says whether the data exists -- "there are none" and "not yours" must be
    indistinguishable, or a refusal becomes a lookup tool.
    """
    principal = _principal(state)
    if principal.role is Role.ANONYMOUS:
        answer = sign_in_required_message()
    else:
        answer = not_authorized_message(state["classification"].domains)
    return {"answer": answer, "not_authorized": True}


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


def _resolve_authorized_customer_ids(db, vendor_id: str) -> list[str] | None:
    """The customers who have ordered from `vendor_id`, or None if there are too many.

    Written in code, not generated: this is an authorization boundary, and the one thing that must
    never depend on a model producing the right filter. It is the same reasoning as
    `app/agents/enrichment.py` -- the query is identical every time, so there is nothing for a
    model to decide.

    Bounded by `RBAC_MAX_AUTHORIZED_IDS`, and returning None past the cap rather than a truncated
    list. A truncated list would silently answer "your customers in Karachi" from an arbitrary
    subset while looking complete, which is worse than refusing -- the caller turns None into a
    message telling the user to narrow the question.
    """
    cap = settings.rbac_max_authorized_ids
    rows = db["orders"].aggregate(
        [
            {"$match": {"vendor_id": vendor_id}},
            {"$group": {"_id": "$customer_id"}},
            # One past the cap, so "exactly at the limit" and "over it" are distinguishable.
            {"$limit": cap + 1},
        ],
        maxTimeMS=settings.mongodb_query_timeout_ms,
    )
    ids = [row["_id"] for row in rows if row.get("_id")]
    if len(ids) > cap:
        return None
    return ids


def _resolve_anchors_node(gemini: GeminiClient):
    def node(state: GraphState) -> dict:
        classification: Classification = state["classification"]
        domains = set(classification.domains)
        principal = _principal(state)
        update: dict = {}

        # Partial refusal: the authorized half of the question still gets answered, and the
        # refused half is reported through the existing per-domain "declined" channel rather than
        # a new one -- app/messages.py::out_of_scope_message already renders that readably, and
        # _synthesize_node already tells the model what was skipped so it can judge whether the
        # gap matters.
        # The same slice _authorized_domains applies, so a domain dropped by the fan-out cap
        # isn't reported to the user as an authorization refusal.
        considered = classification.domains[: settings.agent_max_fan_out]
        denied = [d for d in considered if not may_query(principal, d)]
        if denied:
            update["out_of_scope_by_domain"] = {d: not_authorized_message([d]) for d in denied}

        if any(needs_authorized_customers(principal, d) for d in domains):
            start = time.perf_counter()
            authorized = _resolve_authorized_customer_ids(get_db(), principal.user_id or "")
            if authorized is None:
                update["out_of_scope_by_domain"] = {
                    **update.get("out_of_scope_by_domain", {}),
                    "customers": too_many_related_records_message("customers"),
                }
                # An empty list, not None: forced_filter turns None into an impossible filter, and
                # the domain is being refused above anyway. Leaving it unset would be the one
                # shape that could reach a query with no restriction.
                update["authorized_customer_ids"] = []
            else:
                update["authorized_customer_ids"] = authorized
            update.setdefault("timings", {})["resolve_authorized_customers_ms"] = round(
                (time.perf_counter() - start) * 1000, 2
            )

        if not (classification.needs_geo and "vendors" in domains):
            return update

        _sink(state).stage("locating")
        question = _effective_question(state)
        spec_cache = state.get("spec_cache")
        # Seeded from what the authorization step above already recorded, not a fresh dict: the
        # final `update["timings"] = timings` would otherwise drop it.
        timings: dict[str, float] = dict(update.get("timings", {}))

        start = time.perf_counter()
        customer_location: dict | None = None
        if "customers" in domains:
            _, rows, _, _, _ = _generate_validate_execute(
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
            _, vendor_rows, _, _, _ = _generate_validate_execute(
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


def _authorized_domains(state: GraphState) -> list[str]:
    """The classifier's domains, minus any this principal may not read at all.

    Filtered here, before any Send is issued, so a refused domain costs no Gemini generation call
    and no Mongo round-trip. _resolve_anchors_node records what was refused in
    `out_of_scope_by_domain`, so a two-domain question says which half it couldn't cover rather
    than silently answering half of it.
    """
    principal = _principal(state)
    classification = state["classification"]
    return [
        d for d in classification.domains[: settings.agent_max_fan_out] if may_query(principal, d)
    ]


def _fan_out(state: GraphState) -> list[Send] | str:
    # Already-refused domains are dropped here as well as in _authorized_domains: a vendor whose
    # customer scope came back too large to apply has had `customers` refused by
    # _resolve_anchors_node, and fanning out to it anyway would spend a Gemini generation call and
    # a Mongo round-trip producing rows the answer must then ignore.
    refused = set(state.get("out_of_scope_by_domain") or {})
    domains = [d for d in _authorized_domains(state) if d not in refused]
    if not domains:
        # Everything was refused *after* routing -- the only way here is a scope that had to be
        # computed and came back unusable (a vendor with more customers than the cap). Returning
        # an empty list would end the graph with no `answer` at all, so go straight to synthesize,
        # which renders whatever is in out_of_scope_by_domain. Reachable only in that case:
        # _route_after_classify already sends a wholly-unauthorized question to `deny`.
        return "synthesize"
    sends = []
    for domain_name in domains:
        payload: dict = {
            "question": _effective_question(state),
            "domain": domain_name,
            # Threaded explicitly because a Send payload is a fresh dict, NOT the graph state:
            # a fanned-out node sees only what is put here. Omitting this silently disabled the
            # whole `/login` guardrail -- the id filters (now `_domain_filter`) read the scope
            # from their own node's state, found nothing, and forced none at all, so an
            # authenticated vendor saw every vendor's rows. Nothing failed; the answers were just
            # wrong. `_principal(state)` now defaults a missing one to ANONYMOUS, which reads
            # nothing -- so the same mistake fails loudly instead.
            "principal": _principal(state),
            # Resolved in _resolve_anchors_node. Threaded explicitly for the same reason as
            # everything else here: a Send payload is a fresh dict, and a missing key would make a
            # vendor's customers query fail *open* if forced_filter treated None as "no filter" --
            # which is precisely why it doesn't (see app/security/roles.py).
            "authorized_customer_ids": state.get("authorized_customer_ids"),
            # Shared with _resolve_anchors_node so a domain already queried while resolving a
            # cross-domain anchor (customers/vendors, in the geo composite pattern) doesn't pay
            # for an identical Gemini generation call a second time here.
            "spec_cache": state.get("spec_cache"),
            # Same reason as the principal above: a Send payload is a fresh dict, so a
            # sink left out here means a silently un-streamed fan-out rather than an error.
            "stream_sink": _sink(state),
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


def _domain_filter(state: GraphState, domain_name: str) -> dict | None:
    """The row restriction for this principal and domain, merged into the generated query in code.

    The policy itself lives in `app/security/roles.py` -- this only supplies the two things the
    policy cannot know: which customer ids a vendor is entitled to (resolved in
    `_resolve_anchors_node`), and the anchor ids a cross-domain geo question resolved.

    Both kinds of restriction are merged, not chosen between: a vendor asking "which of my
    customers are near me?" must be limited to *their* customers **and** the geo anchor, and
    keeping only one of the two would answer a question they didn't ask.
    """
    forced: dict = {}

    authorization = forced_filter(
        _principal(state),
        domain_name,
        authorized_customer_ids=state.get("authorized_customer_ids"),
    )
    if authorization:
        forced.update(authorization)

    if domain_name == "orders":
        # Anchors from a cross-domain geo question. Applied only where authorization hasn't
        # already pinned the same field -- a vendor's own vendor_id is not negotiable, and an
        # anchor set must never widen it.
        if "vendor_id" not in forced and state.get("resolved_vendor_ids"):
            forced["vendor_id"] = {"$in": state["resolved_vendor_ids"]}
        if "customer_id" not in forced and state.get("resolved_customer_ids"):
            forced["customer_id"] = {"$in": state["resolved_customer_ids"]}

    return forced or None


def _domain_agent_node(gemini: GeminiClient):
    def node(state: GraphState) -> dict:
        domain_name = state["domain"]
        question = state["question"]
        start = time.perf_counter()
        # Named rather than generic: fanned-out agents run concurrently, so "querying orders" and
        # "querying customers" arriving together is the honest picture of what is happening.
        _sink(state).stage("querying", domain_name)

        geo_override_location = (
            state.get("resolved_customer_location") if domain_name == "vendors" else None
        )

        id_filter = _domain_filter(state, domain_name)

        spec, rows, error, out_of_scope, failure = _generate_validate_execute(
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
                    get_db(),
                    rows,
                    timeout_ms=settings.mongodb_query_timeout_ms,
                    # The question decides whether the *party's* own columns come too -- see
                    # enrichment.wants_related_details. `question` here is already the resolved
                    # one (_fan_out passes it), so a follow-up asking for "their details" works.
                    question=question,
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
                # Raw text for the audit log, classified kind for the reply -- see
                # _generate_validate_execute's docstring for why these are kept apart.
                update["errors_by_domain"] = {domain_name: error}
                update["error_kinds_by_domain"] = {domain_name: (failure or Failure.UNKNOWN).value}
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
                # errors_by_domain holds raw validator/pymongo text for the audit log. What the
                # user gets is the catalogue's phrasing for the kind the failing node already
                # classified, plus a reference code -- the raw text used to be interpolated
                # straight into the reply, putting driver messages into a Slack channel.
                kinds = state.get("error_kinds_by_domain", {})
                kind = next(iter(kinds.values()), Failure.UNKNOWN.value)
                answer = failure_message(Failure(kind), new_reference())
            elif out_of_scope_by_domain:
                # The model's own refusal is more specific than anything generic we could say,
                # so it's shown as written -- this is a deliberate answer, not a fault.
                answer = out_of_scope_message(out_of_scope_by_domain)
            else:
                answer = no_data_message(list(rows_by_domain) or state["classification"].domains)
        else:
            skipped = {**out_of_scope_by_domain, **errors_by_domain}
            sink = _sink(state)
            sink.stage("writing")
            # Streamed only when someone is listening. The Slack path holds NULL_SINK and takes
            # the plain call, so it neither pays for streaming nor changes behaviour -- and
            # `stream_answer` returns the same complete string either way, so everything
            # downstream (the PII scan, the report builder, the cache) is identical.
            if sink is NULL_SINK:
                answer = gemini.generate_answer(
                    _effective_question(state), rows_by_domain, skipped or None
                )
            else:
                answer = gemini.stream_answer(
                    _effective_question(state),
                    rows_by_domain,
                    skipped or None,
                    on_text=sink.token,
                )

        elapsed = round((time.perf_counter() - start) * 1000, 2)
        return {"answer": answer, "timings": {"synthesize_ms": elapsed}}

    return node


def build_graph(gemini: GeminiClient):
    graph = StateGraph(GraphState)
    graph.add_node("classify", _classify_node(gemini))
    graph.add_node("clarify", _clarify_node)
    graph.add_node("deny", _deny_node)
    graph.add_node("confirm_context_switch", _confirm_context_switch_node)
    graph.add_node("resolve_anchors", _resolve_anchors_node(gemini))
    graph.add_node("domain_agent", _domain_agent_node(gemini))
    graph.add_node("synthesize", _synthesize_node(gemini))

    graph.add_edge(START, "classify")
    graph.add_conditional_edges(
        "classify",
        _route_after_classify,
        ["clarify", "confirm_context_switch", "deny", "resolve_anchors"],
    )
    graph.add_edge("clarify", END)
    graph.add_edge("deny", END)
    graph.add_edge("confirm_context_switch", END)
    graph.add_conditional_edges("resolve_anchors", _fan_out, ["domain_agent", "synthesize"])
    graph.add_edge("domain_agent", "synthesize")
    graph.add_edge("synthesize", END)

    return graph.compile()
