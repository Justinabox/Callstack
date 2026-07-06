"""Tests for AT command builders."""

import pytest

from callstack.protocol.commands import ATCommand


@pytest.mark.parametrize("number", ["12+34", "++123", "+123#"])
def test_dial_rejects_embedded_or_repeated_plus_signs(number):
    with pytest.raises(ValueError):
        ATCommand.dial(number)


@pytest.mark.parametrize("number", ["*", "#", "*123", "#123#", "*1##", "**123#"])
def test_dial_rejects_malformed_service_codes(number):
    with pytest.raises(ValueError):
        ATCommand.dial(number)


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        ("5551234", "ATD5551234;"),
        ("+15551234567", "ATD+15551234567;"),
        ("*123#", "ATD*123#;"),
    ],
)
def test_dial_accepts_documented_voice_number_formats(number, expected):
    assert ATCommand.dial(number) == expected


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


@pytest.mark.parametrize("index", [True, False])
def test_sms_storage_commands_reject_boolean_indexes(index):
    with pytest.raises(ValueError):
        ATCommand.read_sms(index)
    with pytest.raises(ValueError):
        ATCommand.delete_sms(index)


@pytest.mark.parametrize(
    ("index", "read_command", "delete_command"),
    [
        (0, "AT+CMGR=0", "AT+CMGD=0"),
        (1, "AT+CMGR=1", "AT+CMGD=1"),
    ],
)
def test_sms_storage_commands_accept_non_negative_integer_indexes(
    index, read_command, delete_command
):
    assert ATCommand.read_sms(index) == read_command
    assert ATCommand.delete_sms(index) == delete_command
