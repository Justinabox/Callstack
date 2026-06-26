"""Tests for privacy-preserving formatting helpers."""

from callstack.privacy import redact_phone_number


def test_redact_phone_number_hides_short_identifier():
    """Short phone-like identifiers should not be fully visible in logs."""
    redacted = redact_phone_number("1234")

    assert "1234" not in redacted
    assert redacted


def test_redact_phone_number_treats_empty_value_as_unknown():
    """Empty phone fields should keep operator logs readable."""
    assert redact_phone_number("") == "unknown"
