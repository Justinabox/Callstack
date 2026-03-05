from dataclasses import dataclass
from typing import Optional


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
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        if self.command_timeout <= 0:
            raise ValueError(f"command_timeout must be positive, got {self.command_timeout}")
        if self.reconnect_interval <= 0:
            raise ValueError(f"reconnect_interval must be positive, got {self.reconnect_interval}")
        if self.baudrate <= 0:
            raise ValueError(f"baudrate must be positive, got {self.baudrate}")
