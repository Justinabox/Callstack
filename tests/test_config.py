"""Tests for config and errors."""

from typing import Any

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
    assert cfg.sms_prompt_timeout == 10.0
    assert cfg.sms_submit_timeout == 30.0
    assert cfg.auto_reconnect is True
    assert cfg.sms_db_path is None


def test_custom_config():
    cfg = ModemConfig(at_port="/dev/ttyACM0", baudrate=9600)
    assert cfg.at_port == "/dev/ttyACM0"
    assert cfg.baudrate == 9600


def test_positional_constructor_preserves_existing_field_order():
    cfg = ModemConfig(
        "/dev/at",
        "/dev/audio",
        9600,
        2.0,
        False,
        7.0,
        "ME",
        "/tmp/sms.sqlite3",
        "1234",
        "DEBUG",
    )

    assert cfg.command_timeout == 2.0
    assert cfg.sms_prompt_timeout == 10.0
    assert cfg.sms_submit_timeout == 30.0
    assert cfg.auto_reconnect is False
    assert cfg.reconnect_interval == 7.0
    assert cfg.sms_storage == "ME"
    assert cfg.sms_db_path == "/tmp/sms.sqlite3"
    assert cfg.sim_pin == "1234"
    assert cfg.log_level == "DEBUG"


@pytest.mark.parametrize(
    ("field", "contract"),
    [
        ("command_timeout", "positive finite number"),
        ("reconnect_interval", "positive finite number"),
        ("baudrate", "non-boolean positive integer"),
    ],
)
@pytest.mark.parametrize("bad_value", [float("nan"), float("inf")])
def test_modem_config_rejects_non_finite_direct_constructor_values(
    field, contract, bad_value
):
    with pytest.raises(ValueError) as excinfo:
        ModemConfig(**{field: bad_value})

    assert field in str(excinfo.value)
    assert contract in str(excinfo.value)


@pytest.mark.parametrize(
    "field",
    [
        "command_timeout",
        "sms_prompt_timeout",
        "sms_submit_timeout",
        "reconnect_interval",
    ],
)
@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_modem_config_rejects_non_finite_timeout_and_reconnect_values(field, bad_value):
    with pytest.raises(ValueError) as excinfo:
        ModemConfig(**{field: bad_value})

    message = str(excinfo.value)
    assert field in message
    assert "positive finite number" in message


@pytest.mark.parametrize(
    "field",
    [
        "command_timeout",
        "sms_prompt_timeout",
        "sms_submit_timeout",
        "reconnect_interval",
    ],
)
@pytest.mark.parametrize("bad_value", [0, -1])
def test_modem_config_rejects_nonpositive_timeout_and_reconnect_values(
    field, bad_value
):
    with pytest.raises(ValueError) as excinfo:
        ModemConfig(**{field: bad_value})

    message = str(excinfo.value)
    assert field in message
    assert "positive finite number" in message


@pytest.mark.parametrize("bad_value", [0, -1, 1.5])
def test_modem_config_rejects_nonpositive_or_noninteger_baudrate(bad_value):
    with pytest.raises(ValueError) as excinfo:
        ModemConfig(baudrate=bad_value)

    message = str(excinfo.value)
    assert "baudrate" in message
    assert "non-boolean positive integer" in message


@pytest.mark.parametrize(
    ("field", "contract"),
    [
        ("command_timeout", "positive finite number"),
        ("sms_prompt_timeout", "positive finite number"),
        ("sms_submit_timeout", "positive finite number"),
        ("reconnect_interval", "positive finite number"),
        ("baudrate", "non-boolean positive integer"),
    ],
)
def test_modem_config_rejects_arbitrary_object_direct_constructor_values(field, contract):
    invalid_value: Any = object()
    with pytest.raises(ValueError) as excinfo:
        ModemConfig(**{field: invalid_value})

    assert field in str(excinfo.value)
    assert contract in str(excinfo.value)


class _UnstringifiableValue:
    def __str__(self) -> str:
        raise RuntimeError("value stringification is unsafe")


@pytest.mark.parametrize(
    ("field", "contract"),
    [
        ("command_timeout", "positive finite number"),
        ("baudrate", "non-boolean positive integer"),
    ],
)
def test_modem_config_rejects_unstringifiable_direct_values(field, contract):
    with pytest.raises(ValueError) as excinfo:
        ModemConfig(**{field: _UnstringifiableValue()})

    message = str(excinfo.value)
    assert field in message
    assert contract in message
    assert "value stringification is unsafe" not in message


@pytest.mark.parametrize("field", ["command_timeout", "reconnect_interval"])
def test_modem_config_normalizes_huge_integer_timeout_overflow(field):
    with pytest.raises(ValueError) as excinfo:
        ModemConfig(**{field: 10**1000})

    message = str(excinfo.value)
    assert field in message
    assert "positive finite number" in message


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("command_timeout", float.fromhex("0x0.0000000000001p-1022")),
        ("sms_prompt_timeout", float.fromhex("0x0.0000000000001p-1022")),
        ("sms_submit_timeout", float.fromhex("0x0.0000000000001p-1022")),
        ("reconnect_interval", float.fromhex("0x0.0000000000001p-1022")),
        ("baudrate", 1),
    ],
)
def test_modem_config_accepts_lowest_positive_direct_constructor_values(field, value):
    config = ModemConfig(**{field: value})

    assert getattr(config, field) == value


@pytest.mark.parametrize(
    "field",
    [
        "command_timeout",
        "sms_prompt_timeout",
        "sms_submit_timeout",
        "reconnect_interval",
        "baudrate",
    ],
)
@pytest.mark.parametrize("bad_value", [True, False, "not-a-number", None])
def test_modem_config_rejects_boolean_and_nonnumeric_direct_constructor_values(
    field, bad_value
):
    with pytest.raises(ValueError, match=field):
        ModemConfig(**{field: bad_value})


def test_load_modem_config_from_env_maps_documented_values_and_secret_indirection():
    cfg = load_modem_config_from_env({
        "CALLSTACK_AT_PORT": "/dev/envAT",
        "CALLSTACK_AUDIO_PORT": "/dev/envAudio",
        "CALLSTACK_BAUDRATE": "9600",
        "CALLSTACK_COMMAND_TIMEOUT": "2.5",
        "CALLSTACK_SMS_PROMPT_TIMEOUT": "4.5",
        "CALLSTACK_SMS_SUBMIT_TIMEOUT": "45.0",
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
    assert cfg.sms_prompt_timeout == 4.5
    assert cfg.sms_submit_timeout == 45.0
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
