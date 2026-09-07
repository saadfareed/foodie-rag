"""One test per field-policy rule, both the pass and the fail case -- mirroring
tests/test_query_validator.py's convention."""

import pytest

from app.security.field_policy import (
    REDACTION_PLACEHOLDER,
    is_internal_field,
    is_secret_field,
    sanitize_document,
    sanitize_rows,
)


@pytest.mark.parametrize(
    "field", ["_id", "__v", "_class", "_index", "id", "ObjectId", "search_index", "shard_idx"]
)
def test_internal_fields_are_recognized(field):
    assert is_internal_field(field)


@pytest.mark.parametrize(
    "field", ["user_id", "vendor_id", "customer_id", "order_id", "name", "city", "status"]
)
def test_business_identifiers_are_not_internal(field):
    """`user_id` and friends are how a human refers to a record -- dropping them would make
    every answer unusable. Only storage plumbing goes."""
    assert not is_internal_field(field)


@pytest.mark.parametrize(
    "field",
    [
        "card_number",
        "credit_card",
        "cvv",
        "password",
        "api_key",
        "secret_key",
        "ssn",
        "auth_token",
        "account_number",
    ],
)
def test_secret_fields_are_recognized(field):
    assert is_secret_field(field)


@pytest.mark.parametrize(
    "field", ["accounts", "occurred", "shipping", "discount", "card_holder_city"]
)
def test_ordinary_fields_are_not_mistaken_for_secrets(field):
    """Word-boundary matching matters: a substring search for "cc" would redact "accounts"."""
    if field == "card_holder_city":
        # "card" appears, so this one *is* redacted -- documented false positive, and redacting
        # too much is the safe direction.
        assert is_secret_field(field)
    else:
        assert not is_secret_field(field)


def test_internal_fields_are_dropped_and_secrets_redacted():
    result = sanitize_document(
        {"_id": "abc", "user_id": "USR-1", "card_number": "4111111111111111"}
    )

    assert "_id" not in result
    assert result["user_id"] == "USR-1"
    assert result["card_number"] == REDACTION_PLACEHOLDER


def test_nested_documents_and_arrays_are_sanitized_too():
    """An `_id` inside `payment` is exactly as much of a leak as a top-level one."""
    result = sanitize_document(
        {
            "user_id": "USR-1",
            "payment": {"_id": "inner", "method": "cash", "cvv": "123"},
            "items": [{"_id": "i1", "sku": "S1"}],
        }
    )

    assert "_id" not in result["payment"]
    assert result["payment"]["method"] == "cash"
    assert result["payment"]["cvv"] == REDACTION_PLACEHOLDER
    assert result["items"] == [{"sku": "S1"}]


def test_sanitize_rows_does_not_mutate_the_input():
    """Rows can be shared across call sites; mutating in place would alter another's view."""
    original = [{"_id": "abc", "user_id": "USR-1"}]

    sanitize_rows(original)

    assert original == [{"_id": "abc", "user_id": "USR-1"}]


def test_extra_denied_fields_from_config_are_honoured(monkeypatch):
    monkeypatch.setattr(
        "app.security.field_policy.settings.security_extra_denied_fields", ["margin"]
    )

    assert is_internal_field("margin")
    assert "margin" not in sanitize_document({"margin": 0.4, "city": "Karachi"})
