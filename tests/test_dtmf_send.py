"""Tests for DTMF send capability."""

import pytest
from callstack.protocol.commands import ATCommand


class TestDTMFSendCommand:
    def test_send_digit(self):
        assert ATCommand.send_dtmf("5") == "AT+VTS=5"

    def test_send_star(self):
        assert ATCommand.send_dtmf("*") == "AT+VTS=*"

    def test_send_hash(self):
        assert ATCommand.send_dtmf("#") == "AT+VTS=#"

    def test_send_letter_a(self):
        assert ATCommand.send_dtmf("A") == "AT+VTS=A"

    def test_send_letter_d(self):
        assert ATCommand.send_dtmf("D") == "AT+VTS=D"

    def test_send_digit_with_duration_encodes_tenths_of_second(self):
        assert ATCommand.send_dtmf("5", duration_ms=300) == "AT+VTS=5,3"

    def test_zero_duration_uses_modem_default(self):
        assert ATCommand.send_dtmf("5", duration_ms=0) == "AT+VTS=5"

    @pytest.mark.parametrize("duration_ms", [-100, 50, 250, 25600, True, False, "300"])
    def test_invalid_duration(self, duration_ms):
        with pytest.raises(ValueError, match="DTMF duration"):
            ATCommand.send_dtmf("5", duration_ms=duration_ms)

    def test_invalid_digit(self):
        with pytest.raises(ValueError):
            ATCommand.send_dtmf("X")

    def test_empty_digit(self):
        with pytest.raises(ValueError):
            ATCommand.send_dtmf("")

    def test_multi_char(self):
        with pytest.raises(ValueError):
            ATCommand.send_dtmf("12")
