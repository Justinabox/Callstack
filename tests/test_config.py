"""Tests for config and errors."""

import pytest

from callstack.config import ModemConfig, load_modem_config_from_env
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


def test_load_modem_config_from_env_maps_documented_values_and_secret_indirection():
    cfg = load_modem_config_from_env({
        "CALLSTACK_AT_PORT": "/dev/envAT",
        "CALLSTACK_AUDIO_PORT": "/dev/envAudio",
        "CALLSTACK_BAUDRATE": "9600",
        "CALLSTACK_COMMAND_TIMEOUT": "2.5",
        "CALLSTACK_AUTO_RECONNECT": "false",
        "CALLSTACK_RECONNECT_INTERVAL": "7.25",
        "CALLSTACK_SMS_STORAGE": "ME",
        "CALLSTACK_SMS_DB_PATH": "/var/lib/callstack/sms.sqlite3",
        "CALLSTACK_SIM_PIN_ENV": "CALLSTACK_SECRET_PIN",
        "CALLSTACK_SECRET_PIN": "1234",
        "CALLSTACK_LOG_LEVEL": "WARNING",
    })

    assert cfg.at_port == "/dev/envAT"
    assert cfg.audio_port == "/dev/envAudio"
    assert cfg.baudrate == 9600
    assert cfg.command_timeout == 2.5
    assert cfg.auto_reconnect is False
    assert cfg.reconnect_interval == 7.25
    assert cfg.sms_storage == "ME"
    assert cfg.sms_db_path == "/var/lib/callstack/sms.sqlite3"
    assert cfg.sim_pin == "1234"
    assert cfg.log_level == "WARNING"


def test_load_modem_config_from_env_rejects_invalid_numbers_without_leaking_secrets():
    with pytest.raises(ValueError) as excinfo:
        load_modem_config_from_env({
            "CALLSTACK_BAUDRATE": "not-a-number",
            "CALLSTACK_SIM_PIN_ENV": "CALLSTACK_SECRET_PIN",
            "CALLSTACK_SECRET_PIN": "1234",
        })

    message = str(excinfo.value)
    assert "CALLSTACK_BAUDRATE" in message
    assert "1234" not in message


@pytest.mark.parametrize("bad_value", ["nan", "inf"])
def test_load_modem_config_from_env_rejects_non_finite_numbers(bad_value):
    with pytest.raises(ValueError) as excinfo:
        load_modem_config_from_env({"CALLSTACK_COMMAND_TIMEOUT": bad_value})

    assert "CALLSTACK_COMMAND_TIMEOUT" in str(excinfo.value)


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
