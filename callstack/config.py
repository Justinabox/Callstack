from dataclasses import dataclass
import math
from typing import Any, Mapping, Optional


class ConfigError(ValueError):
    """Raised when redacted deployment/CLI configuration is invalid."""


@dataclass
class ModemConfig:
    at_port: str = "/dev/ttyUSB2"
    audio_port: str = "/dev/ttyUSB4"
    baudrate: int = 115200
    command_timeout: float = 5.0
    auto_reconnect: bool = True
    reconnect_interval: float = 5.0
    sms_storage: str = "SM"
    sms_db_path: Optional[str] = None
    sim_pin: Optional[str] = None
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        if self.command_timeout <= 0:
            raise ValueError(f"command_timeout must be positive, got {self.command_timeout}")
        if self.reconnect_interval <= 0:
            raise ValueError(f"reconnect_interval must be positive, got {self.reconnect_interval}")
        if self.baudrate <= 0:
            raise ValueError(f"baudrate must be positive, got {self.baudrate}")


def _env_name(prefix: str, suffix: str) -> str:
    return f"{prefix}{suffix}"


def _parse_positive_int(env: Mapping[str, str], name: str) -> int | None:
    value = env.get(name)
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise ConfigError(f"{name} must be a positive integer")
    return parsed


def _parse_positive_float(env: Mapping[str, str], name: str) -> float | None:
    value = env.get(name)
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a positive number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ConfigError(f"{name} must be a positive finite number")
    return parsed


def _parse_bool(env: Mapping[str, str], name: str) -> bool | None:
    value = env.get(name)
    if value is None or value == "":
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean value")


def load_modem_config_from_env(
    env: Mapping[str, str],
    *,
    prefix: str = "CALLSTACK_",
) -> ModemConfig:
    """Build :class:`ModemConfig` from documented environment variables.

    The loader is pure/testable: callers pass the environment mapping. Secret
    values such as SIM PINs are only read through ``{prefix}SIM_PIN_ENV`` and
    are never interpolated into validation errors.
    """

    kwargs: dict[str, Any] = {}
    string_fields = {
        "AT_PORT": "at_port",
        "AUDIO_PORT": "audio_port",
        "SMS_STORAGE": "sms_storage",
        "SMS_DB_PATH": "sms_db_path",
        "LOG_LEVEL": "log_level",
    }
    for suffix, field_name in string_fields.items():
        value = env.get(_env_name(prefix, suffix))
        if value:
            kwargs[field_name] = value

    baudrate = _parse_positive_int(env, _env_name(prefix, "BAUDRATE"))
    if baudrate is not None:
        kwargs["baudrate"] = baudrate

    command_timeout = _parse_positive_float(env, _env_name(prefix, "COMMAND_TIMEOUT"))
    if command_timeout is not None:
        kwargs["command_timeout"] = command_timeout

    reconnect_interval = _parse_positive_float(env, _env_name(prefix, "RECONNECT_INTERVAL"))
    if reconnect_interval is not None:
        kwargs["reconnect_interval"] = reconnect_interval

    auto_reconnect = _parse_bool(env, _env_name(prefix, "AUTO_RECONNECT"))
    if auto_reconnect is not None:
        kwargs["auto_reconnect"] = auto_reconnect

    sim_pin_env = env.get(_env_name(prefix, "SIM_PIN_ENV"))
    if sim_pin_env:
        kwargs["sim_pin"] = env.get(sim_pin_env)

    return ModemConfig(**kwargs)
