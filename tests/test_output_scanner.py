"""The last-resort redaction over model prose, in both its forms.

The streaming form is what these tests mostly exist for. Redaction that runs on finished text is
easy to get right and easy to reason about; redaction that has to release text a chunk at a time,
before the rest of the sentence exists, is neither -- and getting it wrong means a card number
streams out in two clean halves that neither pattern matches.

So the property asserted throughout is the one that actually matters: **streaming any chunking of
a text produces exactly what scanning the whole text produces.**
"""

import pytest

from app.security.output_scanner import StreamingRedactor, scan_output_for_pii


def _stream(text: str, chunk_size: int) -> str:
    redactor = StreamingRedactor()
    out = "".join(redactor.feed(text[i : i + chunk_size]) for i in range(0, len(text), chunk_size))
    return out + redactor.finish()


# --- the finished-text scan ---------------------------------------------------------------------


def test_a_card_number_is_redacted():
    assert "[REDACTED_CARD]" in scan_output_for_pii("card 4111111111111111 was used")


def test_a_spaced_card_number_is_redacted():
    assert "[REDACTED_CARD]" in scan_output_for_pii("card 4111 1111 1111 1111 was used")


def test_an_ssn_is_redacted():
    assert scan_output_for_pii("ssn 123-45-6789") == "ssn [REDACTED_SSN]"


def test_ordinary_numbers_survive():
    """A payment breakdown is legitimate data. Over-redaction makes the answer useless in a way
    nobody reports as a bug -- they just stop trusting it."""
    text = "There were 42 orders totalling 18500 across 7 vendors in 2026."

    assert scan_output_for_pii(text) == text


# --- the streaming scan --------------------------------------------------------------------------

_SAMPLES = [
    "Customer paid with 4111 1111 1111 1111 on Tuesday.",
    "SSN 123-45-6789 appeared in the notes.",
    "Two cards: 4111111111111111 and 5500-0000-0000-0004, plus 42 orders.",
    "No sensitive data here at all, just 18 orders worth 4200.",
    "Trailing card at the very end 4111111111111111",
    "4111111111111111 leading card at the very start.",
    "",
    "12345678901234567890123456789",
]


@pytest.mark.parametrize("text", _SAMPLES)
@pytest.mark.parametrize("chunk_size", [1, 2, 3, 7, 13, 1000])
def test_streaming_matches_scanning_the_whole_text(text, chunk_size):
    """The property that matters, over every chunk boundary. A model streams on token
    boundaries nobody controls, so "works when the chunks happen to line up" is not a guarantee."""
    assert _stream(text, chunk_size) == scan_output_for_pii(text)


def test_a_card_split_across_two_chunks_is_still_redacted():
    """The specific failure that motivates the buffering: scanning each chunk on its own matches
    neither half, and the number streams out in two clean pieces."""
    redactor = StreamingRedactor()
    emitted = redactor.feed("The card 4111 1111 ") + redactor.feed("1111 1111 was used.")
    emitted += redactor.finish()

    assert "4111" not in emitted
    assert "[REDACTED_CARD]" in emitted


def test_text_is_released_as_it_arrives_rather_than_held_to_the_end():
    """Buffering everything would be trivially safe and would also mean no streaming at all."""
    redactor = StreamingRedactor()

    assert redactor.feed("There were forty-two orders ") != ""


def test_digits_are_held_until_something_settles_them():
    """A digit run may still turn into a card number, so it cannot be released yet."""
    redactor = StreamingRedactor()

    assert redactor.feed("4111 1111 1111 1111") == ""


def test_finish_is_idempotent():
    redactor = StreamingRedactor()
    redactor.feed("done.")
    redactor.finish()

    assert redactor.finish() == ""
