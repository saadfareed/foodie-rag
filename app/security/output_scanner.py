import re


def scan_output_for_pii(text: str) -> str:
    """
    Scans the final LLM text output for any leaked PII (like credit cards, emails, SSN)
    and redacts them before sending to Slack.
    """
    # Simple regex for credit cards (13-19 digits)
    cc_pattern = r"\b(?:\d[ -]*?){13,19}\b"
    redacted = re.sub(cc_pattern, "[REDACTED_CARD]", text)

    # Simple regex for SSN
    ssn_pattern = r"\b\d{3}-\d{2}-\d{4}\b"
    redacted = re.sub(ssn_pattern, "[REDACTED_SSN]", redacted)

    # Email redaction (optional, but good for privacy)
    # email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,7}\b'
    # redacted = re.sub(email_pattern, "[REDACTED_EMAIL]", redacted)

    return redacted
