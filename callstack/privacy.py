"""Privacy-preserving formatting helpers for logs and operator output."""

import re

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
