from app.agents.classifier import Classification
from app.agents.graph import build_graph
from app.rag.query_spec import GeoNear, QueryError, QuerySpec


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
    return {"question": question, "user_id": "U1", "channel_id": "C1", **extra}


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

    assert "ran into a problem" in result["answer"]
    assert "orders" in result["errors_by_domain"]


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

    assert "ran into a problem" in result["answer"]
    assert "orders" in result["errors_by_domain"]
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

    assert result["errors_by_domain"]["orders"] == (
        "Gemini is temporarily unavailable -- please try again shortly."
    )
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
