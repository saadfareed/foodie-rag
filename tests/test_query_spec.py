from app.rag.query_spec import QuerySpec


def test_explicit_null_limit_falls_back_to_the_default():
    """Regression test for a production bug: the model emitted "limit": null (likely
    pattern-matching the nearby null fields like start_date/end_date) instead of omitting the
    field or using the example value shown in the prompt. A Pydantic field default only applies
    when the key is absent, not when it's explicitly null, so this hard-failed the whole
    question with a raw ValidationError instead of just using the default like a missing key
    would."""
    spec = QuerySpec(collection="orders", operation="find", limit=None)
    assert spec.limit == 50


def test_explicit_null_filter_falls_back_to_empty_dict():
    spec = QuerySpec(collection="orders", operation="find", filter=None)
    assert spec.filter == {}


def test_explicit_null_pipeline_falls_back_to_empty_list():
    spec = QuerySpec(collection="orders", operation="aggregate", pipeline=None)
    assert spec.pipeline == []


def test_omitted_limit_still_uses_the_default():
    spec = QuerySpec(collection="orders", operation="find")
    assert spec.limit == 50


def test_explicit_limit_value_is_preserved():
    spec = QuerySpec(collection="orders", operation="find", limit=10)
    assert spec.limit == 10


def test_genuinely_optional_fields_still_accept_null():
    spec = QuerySpec(
        collection="orders",
        operation="find",
        projection=None,
        sort=None,
        start_date=None,
        end_date=None,
        geo_near=None,
        requested_radius_m=None,
    )
    assert spec.projection is None
    assert spec.geo_near is None
