"""Last-resort redaction over the model's finished prose.

The field policy (app/security/field_policy.py) already strips secret values as rows leave
MongoDB, so nothing downstream should ever have seen a card number. This is the belt to that
pair of braces: the answer is model-generated text, and a model can restate a number it was
shown.
"""

import re

#: Credit-card-shaped digit runs. The separator class is deliberately narrow -- see
#: _UNSAFE_IN_A_MATCH below, which depends on knowing exactly which characters can appear
#: *inside* a match.
_CARD_PATTERN = re.compile(r"\b(?:\d[ -]*?){13,19}\b")
_SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

#: Every character that can appear inside a match of any pattern above: digits, space, hyphen.
#:
#: This is what makes `StreamingRedactor` safe, so it is not an optimisation detail -- it is the
#: invariant. **A new pattern must either use only these characters, or this set must widen to
#: cover it**, or streaming will emit a split match unredacted. (A pattern matching e.g. email
#: addresses would need letters, `@` and `.` added -- at which point almost nothing is a safe cut
#: and the streaming strategy needs rethinking rather than patching.)
_UNSAFE_IN_A_MATCH = set("0123456789 -")


def scan_output_for_pii(text: str) -> str:
    """Redact anything card- or SSN-shaped from finished text."""
    redacted = _CARD_PATTERN.sub("[REDACTED_CARD]", text)
    return _SSN_PATTERN.sub("[REDACTED_SSN]", redacted)


class StreamingRedactor:
    """Applies the same redaction to text arriving a chunk at a time.

    A streamed answer that went straight to the browser would bypass `scan_output_for_pii`
    entirely -- the scanner runs on the *finished* prose, and by then the tokens have already been
    displayed. Re-scanning each chunk on its own is no fix either: a card number split across two
    chunks matches neither half, so the number streams out in two clean pieces.

    So text is buffered and released only up to a point where a match provably cannot straddle the
    cut: the last character that no pattern can match (anything that isn't a digit, space or
    hyphen -- see `_UNSAFE_IN_A_MATCH`). In ordinary prose that is almost every character, so the
    buffer stays a few characters behind the model rather than holding the answer back.

    `finish()` flushes whatever is left, scanned. Callers must always call it, or the last words
    of every answer are lost.
    """

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: str) -> str:
        """Text safe to display now -- possibly empty, always already redacted."""
        self._buffer += chunk
        cut = self._safe_cut()
        if cut is None:
            return ""
        release, self._buffer = self._buffer[:cut], self._buffer[cut:]
        return scan_output_for_pii(release)

    def finish(self) -> str:
        """The remainder, scanned. Idempotent -- a second call returns nothing."""
        remaining, self._buffer = self._buffer, ""
        return scan_output_for_pii(remaining)

    def _safe_cut(self) -> int | None:
        """One past the last character no pattern can match, or None if there isn't one."""
        for index in range(len(self._buffer) - 1, -1, -1):
            if self._buffer[index] not in _UNSAFE_IN_A_MATCH:
                return index + 1
        return None
