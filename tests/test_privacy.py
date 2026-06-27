"""Tests for privacy-preserving formatting helpers."""

from callstack.privacy import redact_phone_number, redact_url_for_log


def test_redact_phone_number_hides_short_identifier():
    """Short phone-like identifiers should not be fully visible in logs."""
    redacted = redact_phone_number("1234")

    assert "1234" not in redacted
    assert redacted


def test_redact_phone_number_treats_empty_value_as_unknown():
    """Empty phone fields should keep operator logs readable."""
    assert redact_phone_number("") == "unknown"


def test_redact_url_for_log_hides_credentials_query_and_phone_like_path_values():
    """Webhook URLs in logs should retain destination host without secrets."""
    redacted = redact_url_for_log(
        "https://user:pass@hooks.example.test/tenant/15551234567/secret-token?api_key=super-secret&phone=15551234567"
    )

    assert redacted == "https://hooks.example.test/[redacted]?query=redacted"
    assert "user" not in redacted
    assert "pass" not in redacted
    assert "super-secret" not in redacted
    assert "15551234567" not in redacted


def test_redact_url_for_log_does_not_raise_on_malformed_ports():
    """Malformed webhook ports should not break failure logging."""
    redacted = redact_url_for_log("https://hooks.example.test:notaport/path?api_key=super-secret")

    assert redacted == "https://hooks.example.test/[redacted]?query=redacted"
    assert "notaport" not in redacted
    assert "super-secret" not in redacted
