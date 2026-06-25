"""Tests for AT command builders."""

import pytest

from callstack.protocol.commands import ATCommand


@pytest.mark.parametrize("recipient", ["++123", "12+34", "*123#", "+123\n", "+123\r"])
def test_send_sms_rejects_invalid_recipient_strings(recipient):
    with pytest.raises(ValueError):
        ATCommand.send_sms(recipient)


@pytest.mark.parametrize(
    ("recipient", "expected"),
    [
        ("+15551234567", 'AT+CMGS="+15551234567"'),
        ("5551234", 'AT+CMGS="5551234"'),
    ],
)
def test_send_sms_accepts_documented_recipient_formats(recipient, expected):
    assert ATCommand.send_sms(recipient) == expected