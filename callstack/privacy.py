"""Privacy-preserving formatting helpers for logs and operator output."""

import re
from urllib.parse import urlsplit

_DIGIT_RE = re.compile(r"\d")


def redact_phone_number(value: str | None) -> str:
    """Return a display-safe representation of a phone-like identifier.

    The helper preserves enough suffix digits for operator correlation while
    avoiding full phone numbers in default logs. Non-empty non-phone values such
    as ``"unknown"`` are returned unchanged so existing status text remains
    readable.
    """
    if value is None:
        return "unknown"

    text = str(value)
    if not text:
        return "unknown"

    digits = _DIGIT_RE.findall(text)
    if not digits:
        return text

    prefix = "+" if text.lstrip().startswith("+") else ""
    if len(digits) <= 4:
        return f"{prefix}***"

    suffix = "".join(digits[-4:])
    return f"{prefix}***{suffix}"


def redact_url_for_log(value: str | None) -> str:
    """Return a log-safe URL label that keeps scheme/host but drops secrets.

    Webhook URLs commonly carry tenant IDs, tokens, phone numbers, or other
    credentials in userinfo, path segments, and query strings. Logs only need
    enough context to identify the remote host.
    """
    if not value:
        return "unknown-url"

    try:
        parsed = urlsplit(str(value))
    except ValueError:
        return "redacted-url"

    scheme = parsed.scheme or "url"
    hostname = parsed.hostname
    if not hostname:
        return "redacted-url"

    host = hostname
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None:
        host = f"{host}:{port}"

    path = "/[redacted]" if parsed.path and parsed.path != "/" else ""
    query = "?query=redacted" if parsed.query else ""
    return f"{scheme}://{host}{path}{query}"
