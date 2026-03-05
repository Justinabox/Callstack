"""Tests for config and errors."""

from callstack.config import ModemConfig
from callstack.errors import (
    CallstackError,
    ATError,
    ATTimeoutError,
    ATCommandError,
    InvalidStateTransition,
    TransportError,
)


def test_default_config():
    cfg = ModemConfig()
    assert cfg.at_port == "/dev/ttyUSB2"
    assert cfg.audio_port == "/dev/ttyUSB4"
    assert cfg.baudrate == 115200
    assert cfg.command_timeout == 5.0
    assert cfg.auto_reconnect is True
    assert cfg.sms_db_path is None


def test_custom_config():
    cfg = ModemConfig(at_port="/dev/ttyACM0", baudrate=9600)
    assert cfg.at_port == "/dev/ttyACM0"
    assert cfg.baudrate == 9600


def test_error_hierarchy():
    assert issubclass(ATError, CallstackError)
    assert issubclass(ATTimeoutError, ATError)
    assert issubclass(ATCommandError, ATError)
    assert issubclass(TransportError, CallstackError)
    assert issubclass(InvalidStateTransition, CallstackError)


def test_at_command_error():
    err = ATCommandError("AT+INVALID", ["+CME ERROR: 10"])
    assert err.command == "AT+INVALID"
    assert err.error_lines == ["+CME ERROR: 10"]
    assert "AT+INVALID" in str(err)


def test_invalid_state_transition():
    err = InvalidStateTransition("IDLE", "ENDED")
    assert err.from_state == "IDLE"
    assert err.to_state == "ENDED"
    assert "IDLE" in str(err)
    assert "ENDED" in str(err)
