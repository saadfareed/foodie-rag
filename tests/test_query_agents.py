from app.agents.domains import DOMAINS
from app.agents.query_agents import generate_domain_query_spec
from app.rag.query_spec import QueryError, QuerySpec


class _StubGemini:
    def __init__(self, result):
        self._result = result
        self.prompts = []

    def generate_structured_or_error(self, prompt, schema):
        self.prompts.append(prompt)
        assert schema is QuerySpec
        return self._result


def test_generate_domain_query_spec_scopes_the_result_to_the_domain():
    gemini = _StubGemini(QuerySpec(collection="whatever", operation="find", filter={}))

    result = generate_domain_query_spec(gemini, DOMAINS["vendors"], "active vendors")

    assert isinstance(result, QuerySpec)
    assert result.collection == "users"
    assert result.filter == {"usertype": 2}


def test_generate_domain_query_spec_passes_through_query_error_unscoped():
    error = QueryError(error="can't answer that")
    gemini = _StubGemini(error)

    result = generate_domain_query_spec(gemini, DOMAINS["orders"], "what's the weather")

    assert result is error


def test_prompt_is_scoped_to_the_domains_own_fields_only():
    """A customers-domain prompt should never even mention vendor-only fields like `rating` --
    schema scoping is itself a hallucination guardrail (app/rag/schema_context.py)."""
    gemini = _StubGemini(QuerySpec(collection="users", operation="find"))

    generate_domain_query_spec(gemini, DOMAINS["customers"], "active customers")

    prompt = gemini.prompts[0]
    assert "loyalty_tier" in prompt or "No schema information" in prompt
    assert "rating" not in prompt
    assert "business_name" not in prompt


def test_geo_capable_domain_prompt_includes_geo_rule():
    gemini = _StubGemini(QuerySpec(collection="users", operation="find"))
    generate_domain_query_spec(gemini, DOMAINS["vendors"], "vendors nearby")
    assert "geo_near" in gemini.prompts[0]


def test_non_geo_domain_prompt_omits_geo_rule():
    gemini = _StubGemini(QuerySpec(collection="orders", operation="find"))
    generate_domain_query_spec(gemini, DOMAINS["orders"], "pending orders")
    assert "geo_near" not in gemini.prompts[0]


def test_the_orders_prompt_names_the_columns_attached_after_the_query():
    """The orders agent used to refuse "orders with customer details", correctly by its own
    lights: user fields aren't in the orders schema. It refuses no longer because the prompt now
    says those columns arrive afterwards -- and the list comes from the domain, so the prompt and
    the report cannot disagree about what exists."""
    gemini = _StubGemini(QuerySpec(collection="orders", operation="find"))

    generate_domain_query_spec(gemini, DOMAINS["orders"], "incomplete orders with customer details")

    prompt = gemini.prompts[0]
    for column in DOMAINS["orders"].enriched_columns:
        assert column in prompt
    assert "do NOT refuse" in prompt


def test_the_prompt_tells_the_agent_not_to_query_the_attached_columns():
    """They don't exist in the collection, so a filter on one returns nothing -- silently, which
    is the worst way for it to be wrong."""
    gemini = _StubGemini(QuerySpec(collection="orders", operation="find"))

    generate_domain_query_spec(gemini, DOMAINS["orders"], "orders with customer details")

    assert "do NOT try to filter, project or group by them" in gemini.prompts[0]


def test_a_domain_with_nothing_attached_gets_no_such_rule():
    """`customers` and `vendors` read the collection those columns come *from*; telling them
    about an enrichment they don't receive would be noise in the most quota-sensitive prompt."""
    gemini = _StubGemini(QuerySpec(collection="users", operation="find"))

    generate_domain_query_spec(gemini, DOMAINS["customers"], "active customers")

    assert "attached to every row automatically" not in gemini.prompts[0]
