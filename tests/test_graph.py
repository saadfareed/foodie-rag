from app.agents.classifier import Classification
from app.agents.graph import build_graph
from app.rag.query_spec import GeoNear, QueryError, QuerySpec
from app.security.roles import ANONYMOUS, Principal, Role, admin


def vendor(user_id: str):
    """A signed-in vendor, as `/login` and a vendor session token produce."""
    return Principal(role=Role.VENDOR, user_id=user_id)


def customer(user_id: str):
    """A signed-in customer."""
    return Principal(role=Role.CUSTOMER, user_id=user_id)


class _FakeCursor(list):
    def limit(self, n):
        return self

    def max_time_ms(self, n):
        return self


class _FakeCollection:
    def __init__(self, rows):
        self._rows = rows

    def find(self, *args, **kwargs):
        return _FakeCursor(self._rows)


class _FakeDb(dict):
    def __getitem__(self, name):
        return dict.__getitem__(self, name)


def _base_state(question, **extra):
    """Graph state for a question, run as an operator unless a test says otherwise.

    The principal is explicit because the graph refuses an ANONYMOUS one outright -- these tests
    are about routing, fan-out and synthesis, and would otherwise all assert the same refusal.
    The RBAC tests further down pass their own.
    """
    return {
        "question": question,
        "user_id": "U1",
        "channel_id": "C1",
        "principal": admin(),
        **extra,
    }


def test_single_domain_question_returns_a_synthesized_answer(monkeypatch):
    class StubGemini:
        def generate_structured(self, prompt, schema):
            return Classification(domains=["orders"], needs_geo=False, confidence=0.9)

        def generate_structured_or_error(self, prompt, schema):
            return QuerySpec(collection="orders", operation="find")

        def generate_answer(self, question, rows_by_domain, skipped_domains=None):
            return f"answer: {rows_by_domain}"

    monkeypatch.setattr(
        "app.agents.graph.get_db",
        lambda: _FakeDb(orders=_FakeCollection([{"order_id": "ORD-1"}])),
    )

    app = build_graph(StubGemini())
    result = app.invoke(_base_state("how many orders"))

    assert result["answer"] == "answer: {'orders': [{'order_id': 'ORD-1'}]}"
    assert result["specs_by_domain"]["orders"].collection == "orders"
    assert not result.get("needs_clarification")


def test_followup_classification_threads_resolved_question_to_generation_and_synthesis(
    monkeypatch,
):
    """When classify returns context_mode="followup" with a rewritten resolved_question, every
    downstream node (domain agent generation, synthesize) must generate/answer against that
    rewrite -- not the raw, potentially incomplete fragment in state["question"]."""
    seen_generation_prompts = []

    class StubGemini:
        def generate_structured(self, prompt, schema):
            return Classification(
                domains=["orders"],
                needs_geo=False,
                confidence=0.9,
                context_mode="followup",
                resolved_question="what is the total amount of orders vendor V1 had last week?",
            )

        def generate_structured_or_error(self, prompt, schema):
            seen_generation_prompts.append(prompt)
            return QuerySpec(collection="orders", operation="find")

        def generate_answer(self, question, rows_by_domain, skipped_domains=None):
            return f"answered: {question}"

    monkeypatch.setattr(
        "app.agents.graph.get_db",
        lambda: _FakeDb(orders=_FakeCollection([{"amount": 10}])),
    )

    app = build_graph(StubGemini())
    result = app.invoke(
        _base_state(
            "what about the total amount?",
            previous_question="how many orders did vendor V1 have last week?",
        )
    )

    resolved = "what is the total amount of orders vendor V1 had last week?"
    assert result["resolved_question"] == resolved
    assert result["answer"] == f"answered: {resolved}"
    assert any(resolved in prompt for prompt in seen_generation_prompts)


def test_new_topic_classification_leaves_resolved_question_equal_to_the_question(monkeypatch):
    class StubGemini:
        def generate_structured(self, prompt, schema):
            return Classification(domains=["orders"], needs_geo=False, confidence=0.9)

        def generate_structured_or_error(self, prompt, schema):
            return QuerySpec(collection="orders", operation="find")

        def generate_answer(self, question, rows_by_domain, skipped_domains=None):
            return f"answered: {question}"

    monkeypatch.setattr(
        "app.agents.graph.get_db",
        lambda: _FakeDb(orders=_FakeCollection([{"amount": 10}])),
    )

    app = build_graph(StubGemini())
    result = app.invoke(_base_state("how many orders"))

    assert result["resolved_question"] == "how many orders"
    assert result["answer"] == "answered: how many orders"


def test_new_topic_with_live_previous_question_routes_to_confirm_context_switch(monkeypatch):
    """context_mode == "new_topic" alone isn't enough to trigger a confirmation -- it only fires
    when there's actual prior context (state["previous_question"]) that would otherwise be
    silently discarded. No query should be generated and no answer synthesized for the candidate
    question until the user confirms."""

    class StubGemini:
        def generate_structured(self, prompt, schema):
            return Classification(domains=["orders"], needs_geo=False, confidence=0.9)

        def generate_structured_or_error(self, prompt, schema):
            raise AssertionError("no query should be generated before confirmation")

        def generate_answer(self, question, rows_by_domain, skipped_domains=None):
            raise AssertionError("no answer should be synthesized before confirmation")

    app = build_graph(StubGemini())
    result = app.invoke(
        _base_state(
            "how many pending orders are there?",
            previous_question="how many orders did vendor V1 have last week?",
        )
    )

    assert result["needs_context_confirmation"] is True
    assert not result.get("needs_clarification")
    assert "how many orders did vendor V1 have last week?" in result["answer"]
    assert "yes" in result["answer"].lower() and "no" in result["answer"].lower()
    assert not result.get("rows_by_domain")


def test_new_topic_without_a_previous_question_answers_normally(monkeypatch):
    """The default, no-context case: context_mode == "new_topic" with nothing live to discard
    (state["previous_question"] unset) must go straight through to a real answer, exactly as
    before this feature existed."""

    class StubGemini:
        def generate_structured(self, prompt, schema):
            return Classification(domains=["orders"], needs_geo=False, confidence=0.9)

        def generate_structured_or_error(self, prompt, schema):
            return QuerySpec(collection="orders", operation="find")

        def generate_answer(self, question, rows_by_domain, skipped_domains=None):
            return "answered"

    monkeypatch.setattr(
        "app.agents.graph.get_db",
        lambda: _FakeDb(orders=_FakeCollection([{"amount": 10}])),
    )

    app = build_graph(StubGemini())
    result = app.invoke(_base_state("how many orders"))

    assert not result.get("needs_context_confirmation")
    assert result["answer"] == "answered"


def test_followup_with_live_previous_question_skips_confirmation(monkeypatch):
    """context_mode == "followup" must never route to confirm_context_switch, regardless of
    previous_question being set -- that's precisely the case this whole feature answers directly
    instead of discarding."""

    class StubGemini:
        def generate_structured(self, prompt, schema):
            return Classification(
                domains=["orders"],
                needs_geo=False,
                confidence=0.9,
                context_mode="followup",
                resolved_question="what is the total amount of orders vendor V1 had last week?",
            )

        def generate_structured_or_error(self, prompt, schema):
            return QuerySpec(collection="orders", operation="find")

        def generate_answer(self, question, rows_by_domain, skipped_domains=None):
            return "answered"

    monkeypatch.setattr(
        "app.agents.graph.get_db",
        lambda: _FakeDb(orders=_FakeCollection([{"amount": 10}])),
    )

    app = build_graph(StubGemini())
    result = app.invoke(
        _base_state(
            "what about the total amount?",
            previous_question="how many orders did vendor V1 have last week?",
        )
    )

    assert not result.get("needs_context_confirmation")
    assert result["answer"] == "answered"


def test_low_confidence_classification_routes_to_clarify(monkeypatch):
    class StubGemini:
        def generate_structured(self, prompt, schema):
            return Classification(
                domains=[], needs_geo=False, confidence=0.1, clarification_question="which one?"
            )

        def generate_structured_or_error(self, prompt, schema):
            raise AssertionError("should not generate a query when clarification is needed")

        def generate_answer(self, question, rows_by_domain, skipped_domains=None):
            raise AssertionError("should not synthesize an answer when clarification is needed")

    app = build_graph(StubGemini())
    result = app.invoke(_base_state("show me active ones nearby"))

    assert result["answer"] == "which one?"
    assert result["needs_clarification"] is True


def test_out_of_scope_question_surfaces_the_domain_agents_own_message(monkeypatch):
    class StubGemini:
        def generate_structured(self, prompt, schema):
            return Classification(domains=["orders"], needs_geo=False, confidence=0.9)

        def generate_structured_or_error(self, prompt, schema):
            return QueryError(error="I don't have data for that")

        def generate_answer(self, question, rows_by_domain, skipped_domains=None):
            raise AssertionError("should not be called when nothing was queryable")

    app = build_graph(StubGemini())
    result = app.invoke(_base_state("what's the weather"))

    assert result["answer"] == "I don't have data for that"


def test_skipped_domain_reason_is_passed_to_generate_answer_not_appended(monkeypatch):
    """Regression test for a production bug: a domain that declined out of scope used to get an
    unconditional "(Note: I couldn't query X, so that part may be incomplete.)" appended after
    generate_answer ran, regardless of whether X's data was actually needed -- e.g. "sum of
    amount each vendor got" was fully answered from orders data alone (vendor_id identifies the
    vendor), but still got told it "may be incomplete". The reason must be handed to
    generate_answer as context instead, so the model can judge whether it's actually relevant."""

    captured = {}

    class StubGemini:
        def generate_structured(self, prompt, schema):
            return Classification(domains=["orders", "vendors"], needs_geo=False, confidence=0.9)

        def generate_structured_or_error(self, prompt, schema):
            if "'vendors' domain" in prompt:
                return QueryError(error="orders/amounts are outside the vendors schema")
            return QuerySpec(collection="orders", operation="find")

        def generate_answer(self, question, rows_by_domain, skipped_domains=None):
            captured["skipped_domains"] = skipped_domains
            return "vendor V1 got 10"

    monkeypatch.setattr(
        "app.agents.graph.get_db",
        lambda: _FakeDb(orders=_FakeCollection([{"vendor_id": "V1", "total_amount": 10}])),
    )

    app = build_graph(StubGemini())
    result = app.invoke(_base_state("sum of amount each vendor got?"))

    assert result["answer"] == "vendor V1 got 10"
    assert captured["skipped_domains"] == {
        "vendors": "orders/amounts are outside the vendors schema"
    }


def test_execution_error_surfaces_as_a_problem_message(monkeypatch):
    class StubGemini:
        def generate_structured(self, prompt, schema):
            return Classification(domains=["orders"], needs_geo=False, confidence=0.9)

        def generate_structured_or_error(self, prompt, schema):
            return QuerySpec(collection="orders", operation="find")

        def generate_answer(self, question, rows_by_domain, skipped_domains=None):
            raise AssertionError("should not be called on a DB error")

    class _BrokenCollection:
        def find(self, *a, **kw):
            raise RuntimeError("connection refused")

    monkeypatch.setattr("app.agents.graph.get_db", lambda: _FakeDb(orders=_BrokenCollection()))

    app = build_graph(StubGemini())
    result = app.invoke(_base_state("how many orders"))

    assert "Something went wrong" in result["answer"]
    # The raw driver text stays in the audit log and out of the reply.
    assert "connection refused" in result["errors_by_domain"]["orders"]
    assert "connection refused" not in result["answer"]


def test_generation_time_exception_is_a_domain_error_not_a_friendly_rejection(monkeypatch):
    """Regression test for a production bug: generate_structured_or_error raising (e.g. a
    Pydantic ValidationError from a malformed model response) used to propagate all the way out
    of the graph uncaught, killing the whole answer -- including any *other* domain that had
    already succeeded -- and leaking the raw exception text. It must be treated as a genuine
    per-domain error (errors_by_domain), not the friendly out_of_scope_by_domain path that a
    deliberate QueryError gets."""

    class StubGemini:
        def generate_structured(self, prompt, schema):
            return Classification(domains=["orders"], needs_geo=False, confidence=0.9)

        def generate_structured_or_error(self, prompt, schema):
            raise ValueError("1 validation error for QuerySpec\nlimit\n  ...")

        def generate_answer(self, question, rows_by_domain, skipped_domains=None):
            raise AssertionError("should not be called when generation itself failed")

    app = build_graph(StubGemini())
    result = app.invoke(_base_state("how many orders"))

    assert "Something went wrong" in result["answer"]
    assert "orders" in result["errors_by_domain"]
    assert "validation error" not in result["answer"]
    assert "orders" not in result.get("out_of_scope_by_domain", {})


def test_circuit_breaker_error_during_generation_gets_the_friendly_message():
    """Regression test for a production incident: a per-domain generation call raising
    CircuitBreakerOpenError (mid-fan-out, after classification already succeeded) used to leak
    the raw internal message ("Gemini circuit breaker open after 5 consecutive failures --
    failing fast instead of retrying.") straight into the user-facing answer. It should read the
    same as the top-level circuit-breaker message in app/rag/pipeline.py, not expose internals."""
    from app.llm.circuit_breaker import CircuitBreakerOpenError

    class StubGemini:
        def generate_structured(self, prompt, schema):
            return Classification(domains=["orders"], needs_geo=False, confidence=0.9)

        def generate_structured_or_error(self, prompt, schema):
            raise CircuitBreakerOpenError(
                "Gemini circuit breaker open after 5 consecutive failures -- failing fast "
                "instead of retrying."
            )

        def generate_answer(self, question, rows_by_domain, skipped_domains=None):
            raise AssertionError("should not be called when generation itself failed")

    app = build_graph(StubGemini())
    result = app.invoke(_base_state("how many orders"))

    # The raw breaker text is kept for the audit log, and the *kind* is recorded alongside it so
    # synthesize doesn't have to re-derive it by string-matching (which is how a Gemini rate
    # limit once came out phrased as a database error).
    assert "circuit breaker" in result["errors_by_domain"]["orders"]
    assert result["error_kinds_by_domain"]["orders"] == "upstream_unavailable"

    assert "can't reach the service" in result["answer"]
    assert "circuit breaker" not in result["answer"]
    assert "consecutive failures" not in result["answer"]


def test_cross_domain_vendor_near_customer_with_pending_orders(monkeypatch):
    """The named cross-domain pattern from the design: resolve the customer's coordinates in
    code, feed them into the vendor agent's geo_near, then feed the resolved vendor ids into the
    order agent's filter -- none of this is a single model-authored $lookup."""

    class StubGemini:
        def generate_structured(self, prompt, schema):
            return Classification(
                domains=["customers", "vendors", "orders"], needs_geo=True, confidence=0.9
            )

        def generate_structured_or_error(self, prompt, schema):
            if "'customers' domain" in prompt:
                return QuerySpec(collection="users", operation="find", filter={"user_id": "USR-1"})
            if "'vendors' domain" in prompt:
                return QuerySpec(collection="users", operation="find")
            return QuerySpec(collection="orders", operation="find", filter={"status": "pending"})

        def generate_answer(self, question, rows_by_domain, skipped_domains=None):
            return "done"

    class _UsersCollection:
        def find(self, filter_, *a, **kw):
            flat: dict = {}
            for clause in filter_.get("$and", [filter_]):
                flat.update(clause)
            if flat.get("usertype") == 1:
                location = {"type": "Point", "coordinates": [10.0, 20.0]}
                return _FakeCursor([{"user_id": "USR-1", "location": location}])
            return _FakeCursor([{"user_id": "VEN-1", "business_name": "Shop"}])

    class _OrdersCollection:
        def find(self, *a, **kw):
            return _FakeCursor([{"order_id": "ORD-1", "vendor_id": "VEN-1"}])

    monkeypatch.setattr(
        "app.agents.graph.get_db",
        lambda: _FakeDb(users=_UsersCollection(), orders=_OrdersCollection()),
    )

    app = build_graph(StubGemini())
    result = app.invoke(_base_state("vendors near customer USR-1 with pending orders"))

    assert result["resolved_customer_location"] == {"longitude": 10.0, "latitude": 20.0}
    assert result["resolved_vendor_ids"] == ["VEN-1"]
    vendor_spec = result["specs_by_domain"]["vendors"]
    assert vendor_spec.geo_near == GeoNear(
        field="location", longitude=10.0, latitude=20.0, max_distance_m=50_000
    )
    orders_spec = result["specs_by_domain"]["orders"]
    forced = orders_spec.filter["$and"][0]
    assert forced["vendor_id"] == {"$in": ["VEN-1"]}
    assert result["answer"] == "done"


def test_cross_domain_pattern_generates_each_domain_spec_only_once(monkeypatch):
    """Regression test: _resolve_anchors_node resolves the customer's coordinates (a "customers"
    domain query) and then the nearby vendors (a "vendors" domain query) *before* the fan-out
    runs its own "customers" and "vendors" domain_agent nodes for the same question -- without
    the spec_cache dedup in _generate_validate_execute, each of those two domains would generate
    an identical QuerySpec via Gemini twice per question (once for the anchor, once for the
    fan-out), differing only in the limit applied afterward. Counts generation calls per domain
    to prove that no longer happens."""

    generation_calls: dict[str, int] = {"customers": 0, "vendors": 0, "orders": 0}

    class StubGemini:
        def generate_structured(self, prompt, schema):
            return Classification(
                domains=["customers", "vendors", "orders"], needs_geo=True, confidence=0.9
            )

        def generate_structured_or_error(self, prompt, schema):
            if "'customers' domain" in prompt:
                generation_calls["customers"] += 1
                return QuerySpec(collection="users", operation="find", filter={"user_id": "USR-1"})
            if "'vendors' domain" in prompt:
                generation_calls["vendors"] += 1
                return QuerySpec(collection="users", operation="find")
            generation_calls["orders"] += 1
            return QuerySpec(collection="orders", operation="find", filter={"status": "pending"})

        def generate_answer(self, question, rows_by_domain, skipped_domains=None):
            return "done"

    class _UsersCollection:
        def find(self, filter_, *a, **kw):
            flat: dict = {}
            for clause in filter_.get("$and", [filter_]):
                flat.update(clause)
            if flat.get("usertype") == 1:
                location = {"type": "Point", "coordinates": [10.0, 20.0]}
                return _FakeCursor([{"user_id": "USR-1", "location": location}])
            return _FakeCursor([{"user_id": "VEN-1", "business_name": "Shop"}])

    class _OrdersCollection:
        def find(self, *a, **kw):
            return _FakeCursor([{"order_id": "ORD-1", "vendor_id": "VEN-1"}])

    monkeypatch.setattr(
        "app.agents.graph.get_db",
        lambda: _FakeDb(users=_UsersCollection(), orders=_OrdersCollection()),
    )

    app = build_graph(StubGemini())
    result = app.invoke(_base_state("vendors near customer USR-1 with pending orders"))

    assert generation_calls == {"customers": 1, "vendors": 1, "orders": 1}
    # The final answer is unaffected by the dedup -- still built from real, validated rows.
    assert result["answer"] == "done"
    assert result["specs_by_domain"]["customers"].filter["$and"][1] == {"user_id": "USR-1"}


def test_fan_out_is_capped_at_agent_max_fan_out(monkeypatch):
    monkeypatch.setattr("app.config.settings.agent_max_fan_out", 1)

    class StubGemini:
        def generate_structured(self, prompt, schema):
            return Classification(domains=["orders", "vendors"], needs_geo=False, confidence=0.9)

        def generate_structured_or_error(self, prompt, schema):
            return QuerySpec(collection="orders", operation="find")

        def generate_answer(self, question, rows_by_domain, skipped_domains=None):
            return "ok"

    monkeypatch.setattr("app.agents.graph.get_db", lambda: _FakeDb(orders=_FakeCollection([])))

    app = build_graph(StubGemini())
    result = app.invoke(_base_state("orders and vendors"))

    assert set(result["rows_by_domain"]) == {"orders"}


# --- name enrichment in the fan-out ----------------------------------------------------------
#
# Wiring tests. app/agents/enrichment.py's own behaviour is covered in tests/test_enrichment.py;
# what matters here is that the domain agent actually calls it, and that a failure to resolve
# names never costs the user their answer.


class _EnrichableDb(dict):
    """Serves order rows carrying ids, plus the `users` lookup that resolves them."""

    def __init__(self, order_rows, user_rows=None, users_raises=False):
        super().__init__()
        self._order_rows = order_rows
        self._user_rows = user_rows if user_rows is not None else []
        self._users_raises = users_raises

    def __getitem__(self, name):
        if name == "users":
            if self._users_raises:
                raise RuntimeError("users lookup exploded")
            return _FakeCollection(self._user_rows)
        return _FakeCollection(self._order_rows)


class _OrdersGemini:
    def generate_structured(self, prompt, schema):
        return Classification(domains=["orders"], needs_geo=False, confidence=0.9)

    def generate_structured_or_error(self, prompt, schema):
        return QuerySpec(collection="orders", operation="find")

    def generate_answer(self, question, rows_by_domain, skipped_domains=None):
        return "answered"


def test_order_rows_are_enriched_with_customer_and_vendor_names(monkeypatch):
    monkeypatch.setattr(
        "app.agents.graph.get_db",
        lambda: _EnrichableDb(
            [{"order_id": "ORD-1", "customer_id": "USR-1", "vendor_id": "USR-2"}],
            [
                {"user_id": "USR-1", "name": "Ayesha Khan"},
                {"user_id": "USR-2", "name": "Bilal Aslam", "business_name": "Al-Noor Restaurant"},
            ],
        ),
    )
    graph = build_graph(_OrdersGemini())

    result = graph.invoke(_base_state("last 10 orders"))

    row = result["rows_by_domain"]["orders"][0]
    assert row["customer_name"] == "Ayesha Khan"
    assert row["vendor_name"] == "Al-Noor Restaurant"
    assert "customer_id" not in row


def test_a_failed_name_lookup_still_answers_the_question(monkeypatch, caplog):
    """The rows are already a correct answer. Losing the whole question over a presentation-only
    lookup would be a worse outcome than a report that shows ids."""
    import logging

    monkeypatch.setattr(
        "app.agents.graph.get_db",
        lambda: _EnrichableDb([{"order_id": "ORD-1", "customer_id": "USR-1"}], users_raises=True),
    )
    graph = build_graph(_OrdersGemini())

    with caplog.at_level(logging.WARNING, logger="audit"):
        result = graph.invoke(_base_state("last 10 orders"))

    assert result["answer"] == "answered"
    assert result["rows_by_domain"]["orders"][0]["customer_id"] == "USR-1"
    assert "name_enrichment_failed" in caplog.text


def test_rows_without_ids_are_passed_through_untouched(monkeypatch):
    """An aggregation grouped by status has nothing to resolve."""
    monkeypatch.setattr(
        "app.agents.graph.get_db",
        lambda: _EnrichableDb([{"status": "pending", "count": 4}], users_raises=True),
    )
    graph = build_graph(_OrdersGemini())

    result = graph.invoke(_base_state("orders by status"))

    assert result["rows_by_domain"]["orders"] == [{"status": "pending", "count": 4}]


def test_a_validation_failure_surfaces_as_a_domain_error_not_a_crash(monkeypatch):
    """A spec the validator refuses (here: a filter on a restricted field) must come back as a
    scoped error the user can be told about, not escape and fail the whole question."""
    monkeypatch.setattr(
        "app.agents.graph.get_db", lambda: _FakeDb(orders=_FakeCollection([{"a": 1}]))
    )

    class StubGemini:
        def generate_structured(self, prompt, schema):
            return Classification(domains=["orders"], needs_geo=False, confidence=0.9)

        def generate_structured_or_error(self, prompt, schema):
            return QuerySpec(
                collection="orders", operation="find", filter={"card_number": {"$exists": True}}
            )

        def generate_answer(self, question, rows_by_domain, skipped_domains=None):
            raise AssertionError("synthesize should not be asked to describe zero rows")

    result = build_graph(StubGemini()).invoke(_base_state("show me card numbers"))

    assert "card_number" in result["errors_by_domain"]["orders"]
    assert result["rows_by_domain"]["orders"] == []
    assert "wasn't one I'm allowed to run" in result["answer"]
    # The rejected field name is internal -- it belongs in the log, not the reply.
    assert "card_number" not in result["answer"]


def test_a_model_requested_clarification_routes_to_clarify(monkeypatch):
    """High confidence, a real domain -- but the model still asked a question, so answering
    anyway would ignore the one signal that says it isn't sure."""

    class StubGemini:
        def generate_structured(self, prompt, schema):
            return Classification(
                domains=["orders"],
                needs_geo=False,
                confidence=0.95,
                clarification_question="which vendor did you mean?",
            )

        def generate_structured_or_error(self, prompt, schema):
            raise AssertionError("no query should be generated before clarifying")

        def generate_answer(self, question, rows_by_domain, skipped_domains=None):
            raise AssertionError("no answer should be synthesized before clarifying")

    result = build_graph(StubGemini()).invoke(_base_state("orders for that vendor"))

    assert result["needs_clarification"] is True
    assert result["answer"] == "which vendor did you mean?"


# --- RBAC: an authenticated vendor only sees their own rows ----------------------------------
#
# The forced filter is the whole point of `/login`. Untested, a refactor could drop it and every
# vendor would silently see the entire workspace's data.


def _capture_spec_gemini(domains):
    class StubGemini:
        def __init__(self):
            self.specs = []

        def generate_structured(self, prompt, schema):
            return Classification(domains=domains, needs_geo=False, confidence=0.9)

        def generate_structured_or_error(self, prompt, schema):
            return QuerySpec(collection="orders", operation="find")

        def generate_answer(self, question, rows_by_domain, skipped_domains=None):
            return "answered"

    return StubGemini()


def test_an_authenticated_vendor_only_sees_their_own_orders(monkeypatch):
    captured = {}

    class _CapturingCollection(_FakeCollection):
        def find(self, filter_=None, *args, **kwargs):
            captured["filter"] = filter_
            return _FakeCursor(self._rows)

    monkeypatch.setattr(
        "app.agents.graph.get_db",
        lambda: _FakeDb(orders=_CapturingCollection([{"order_id": "ORD-1"}])),
    )

    build_graph(_capture_spec_gemini(["orders"])).invoke(
        _base_state("how many orders do i have?", principal=vendor("USR-42"))
    )

    assert "USR-42" in str(captured["filter"]), captured["filter"]


def test_an_authenticated_vendor_only_sees_their_own_vendor_record(monkeypatch):
    captured = {}

    class _CapturingCollection(_FakeCollection):
        def find(self, filter_=None, *args, **kwargs):
            captured["filter"] = filter_
            return _FakeCursor(self._rows)

    monkeypatch.setattr(
        "app.agents.graph.get_db",
        lambda: _FakeDb(users=_CapturingCollection([{"user_id": "USR-42"}])),
    )

    class StubGemini:
        def generate_structured(self, prompt, schema):
            return Classification(domains=["vendors"], needs_geo=False, confidence=0.9)

        def generate_structured_or_error(self, prompt, schema):
            return QuerySpec(collection="users", operation="find")

        def generate_answer(self, question, rows_by_domain, skipped_domains=None):
            return "answered"

    build_graph(StubGemini()).invoke(_base_state("what's my rating?", principal=vendor("USR-42")))

    assert "USR-42" in str(captured["filter"]), captured["filter"]


def test_an_admin_question_is_not_scoped_to_anyone(monkeypatch):
    captured = {}

    class _CapturingCollection(_FakeCollection):
        def find(self, filter_=None, *args, **kwargs):
            captured["filter"] = filter_
            return _FakeCursor(self._rows)

    monkeypatch.setattr(
        "app.agents.graph.get_db",
        lambda: _FakeDb(orders=_CapturingCollection([{"order_id": "ORD-1"}])),
    )

    build_graph(_capture_spec_gemini(["orders"])).invoke(_base_state("how many orders?"))

    assert "vendor_id" not in str(captured["filter"])
    assert "customer_id" not in str(captured["filter"])


# --- role-based access: every one of these fails silently if it regresses ------------------------
#
# Nothing raises when a forced filter goes missing. The query runs, the answer is fluent, and it
# is built from rows the asker was never entitled to see -- which is why each of these asserts the
# *effect* on the query that reached the database, not that some function was called.


def _flat(filter_):
    """Flatten the `$and` that merge_forced_filter builds.

    A forced filter is combined with the domain's own `usertype` clause rather than merged into
    one dict -- two clauses on the same field must both hold, and flattening them at write time
    would let one silently overwrite the other. Tests assert on the flattened view.
    """
    flat = {}
    for clause in filter_.get("$and", [filter_]):
        flat.update(clause)
    return flat


def _capture_orders(monkeypatch, rows=None):
    captured = {}

    class _CapturingCollection(_FakeCollection):
        def find(self, filter_=None, *args, **kwargs):
            captured["filter"] = filter_
            return _FakeCursor(self._rows)

    monkeypatch.setattr(
        "app.agents.graph.get_db",
        lambda: _FakeDb(orders=_CapturingCollection(rows or [{"order_id": "ORD-1"}])),
    )
    return captured


def _capture_users(monkeypatch, rows=None, orders=None):
    captured = {}

    class _CapturingUsers(_FakeCollection):
        def find(self, filter_=None, *args, **kwargs):
            captured["filter"] = filter_
            return _FakeCursor(self._rows)

    class _OrdersWithCustomers(_FakeCollection):
        def aggregate(self, pipeline, **kwargs):
            captured["orders_pipeline"] = pipeline
            return iter(orders or [])

    monkeypatch.setattr(
        "app.agents.graph.get_db",
        lambda: _FakeDb(
            users=_CapturingUsers(rows or [{"user_id": "USR-C1"}]),
            orders=_OrdersWithCustomers([]),
        ),
    )
    return captured


def _users_gemini(domain):
    class StubGemini:
        def generate_structured(self, prompt, schema):
            return Classification(domains=[domain], needs_geo=False, confidence=0.9)

        def generate_structured_or_error(self, prompt, schema):
            return QuerySpec(collection="users", operation="find")

        def generate_answer(self, question, rows_by_domain, skipped_domains=None):
            return "answered"

    return StubGemini()


def test_a_customer_only_sees_their_own_orders(monkeypatch):
    captured = _capture_orders(monkeypatch)

    build_graph(_capture_spec_gemini(["orders"])).invoke(
        _base_state("how many orders do i have?", principal=customer("USR-C9"))
    )

    assert _flat(captured["filter"]).get("customer_id") == "USR-C9"


def test_a_customer_cannot_list_other_customers(monkeypatch):
    """The customers domain has no natural restriction of its own -- before roles there was no
    filter on it at all, so any authenticated caller could enumerate every customer."""
    captured = _capture_users(monkeypatch)

    build_graph(_users_gemini("customers")).invoke(
        _base_state("show me all customers", principal=customer("USR-C9"))
    )

    assert _flat(captured["filter"]).get("user_id") == "USR-C9"


def test_a_vendor_sees_only_the_customers_who_ordered_from_them(monkeypatch):
    """Resolved from their own orders in code, before the fan-out -- never by asking the model to
    remember to filter."""
    captured = _capture_users(monkeypatch, orders=[{"_id": "USR-C1"}, {"_id": "USR-C2"}])

    build_graph(_users_gemini("customers")).invoke(
        _base_state("who are my customers?", principal=vendor("USR-V1"))
    )

    assert _flat(captured["filter"]).get("user_id") == {"$in": ["USR-C1", "USR-C2"]}
    # And the lookup that produced it was scoped to this vendor's own orders.
    assert captured["orders_pipeline"][0] == {"$match": {"vendor_id": "USR-V1"}}


def test_a_vendor_with_no_customers_matches_nothing_rather_than_everything(monkeypatch):
    """The failure direction that matters. An empty resolved set must stay an empty result, not
    collapse into an unfiltered query over every customer in the database."""
    captured = _capture_users(monkeypatch, orders=[])

    build_graph(_users_gemini("customers")).invoke(
        _base_state("who are my customers?", principal=vendor("USR-V1"))
    )

    assert _flat(captured["filter"]).get("user_id") == {"$in": []}


def test_too_many_customers_refuses_instead_of_truncating(monkeypatch):
    """A truncated scope would answer "your customers in Karachi" from an arbitrary subset while
    looking complete -- worse than refusing, because nothing about it looks wrong."""
    monkeypatch.setattr("app.config.settings.rbac_max_authorized_ids", 2)
    captured = _capture_users(monkeypatch, orders=[{"_id": f"USR-C{i}"} for i in range(3)])

    answer = build_graph(_users_gemini("customers")).invoke(
        _base_state("who are my customers?", principal=vendor("USR-V1"))
    )["answer"]

    assert "narrow" in answer.lower() or "date range" in answer.lower()
    assert "filter" not in captured, "a refused domain was queried anyway"


def test_an_anonymous_question_is_refused_without_touching_gemini_or_mongo(monkeypatch):
    """Refused at routing, before a query is generated: a question this principal may not have
    answered should cost nothing at all."""

    def _explode():
        raise AssertionError("an anonymous question reached the database")

    monkeypatch.setattr("app.agents.graph.get_db", _explode)

    class StubGemini:
        def __init__(self):
            self.generate_calls = 0

        def generate_structured(self, prompt, schema):
            return Classification(domains=["orders"], needs_geo=False, confidence=0.9)

        def generate_structured_or_error(self, prompt, schema):
            self.generate_calls += 1
            raise AssertionError("an anonymous question generated a query")

        def generate_answer(self, question, rows_by_domain, skipped_domains=None):
            raise AssertionError("an anonymous question was synthesized")

    gemini = StubGemini()
    result = build_graph(gemini).invoke(_base_state("how many orders?", principal=ANONYMOUS))

    assert result["not_authorized"] is True
    assert "signed-in" in result["answer"]
    assert gemini.generate_calls == 0


def test_a_partly_refused_question_still_answers_the_authorized_half(monkeypatch):
    """A two-domain question where one domain is off-limits should answer the other and say so,
    rather than refusing wholesale or silently answering half."""

    monkeypatch.setattr("app.agents.graph.may_query", lambda principal, domain: domain != "vendors")
    captured = _capture_orders(monkeypatch)

    result = build_graph(_capture_spec_gemini(["orders", "vendors"])).invoke(
        _base_state("my orders and nearby vendors", principal=customer("USR-C9"))
    )

    assert "orders" in result["rows_by_domain"]
    assert "vendors" in result["out_of_scope_by_domain"]
    assert _flat(captured["filter"]).get("customer_id") == "USR-C9"


def test_an_authorization_filter_is_not_widened_by_a_geo_anchor(monkeypatch):
    """A vendor asking "which of my customers are nearby?" must be limited to their own orders
    *and* the anchor. Letting the anchor overwrite the authorization filter would answer a
    different, much broader question."""
    captured = _capture_orders(monkeypatch)

    build_graph(_capture_spec_gemini(["orders"])).invoke(
        _base_state(
            "my orders near those vendors",
            principal=vendor("USR-V1"),
            resolved_vendor_ids=["USR-V2", "USR-V3"],
        )
    )

    assert _flat(captured["filter"]).get("vendor_id") == "USR-V1"


def test_multiple_out_of_scope_domains_are_all_named(monkeypatch):
    """With one refusal the model's own wording is shown verbatim; with several the answer has
    to say which domain said what, or it reads as one incoherent sentence."""
    monkeypatch.setattr("app.agents.graph.get_db", lambda: _FakeDb())

    class StubGemini:
        def generate_structured(self, prompt, schema):
            return Classification(domains=["orders", "vendors"], needs_geo=False, confidence=0.9)

        def generate_structured_or_error(self, prompt, schema):
            return QueryError(error="no such field here")

        def generate_answer(self, question, rows_by_domain, skipped_domains=None):
            raise AssertionError("nothing to synthesize")

    answer = build_graph(StubGemini()).invoke(_base_state("something odd"))["answer"]

    assert "orders:" in answer and "vendors:" in answer


def test_a_rate_limited_generation_gets_a_clean_per_domain_message(monkeypatch):
    """A mid-fan-out failure should read the way a whole-question failure does, not leak
    Google's raw quota payload into the Slack answer."""
    from google.genai import errors

    monkeypatch.setattr("app.agents.graph.get_db", lambda: _FakeDb())

    class StubGemini:
        def generate_structured(self, prompt, schema):
            return Classification(domains=["orders"], needs_geo=False, confidence=0.9)

        def generate_structured_or_error(self, prompt, schema):
            raise errors.ClientError(
                429, {"error": {"code": 429, "message": "Quota exceeded", "status": "RESOURCE"}}
            )

        def generate_answer(self, question, rows_by_domain, skipped_domains=None):
            raise AssertionError("nothing to synthesize")

    answer = build_graph(StubGemini()).invoke(_base_state("how many orders?"))["answer"]

    assert "rate-limited" in answer
    assert "RESOURCE" not in answer
