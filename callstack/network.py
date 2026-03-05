"""Network service: signal quality, registration status, operator info."""

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Optional

from callstack.events.bus import EventBus
from callstack.events.types import SignalQualityEvent
from callstack.protocol.commands import ATCommand
from callstack.protocol.executor import ATCommandExecutor
from callstack.protocol.parser import ATResponseParser
from callstack.utils.signal_quality import rssi_to_dbm, rssi_to_description

logger = logging.getLogger("callstack.network")

# +COPS: mode,format,"operator_name"
_COPS_RE = re.compile(r'^\+COPS:\s*\d+,\d+,"([^"]*)"')


@dataclass
class SignalInfo:
    """Snapshot of modem signal quality."""
    rssi: int
    ber: int
    dbm: int | None
    description: str


@dataclass
class RegistrationInfo:
    """Network registration status."""
    status: int  # 0=not registered, 1=home, 2=searching, 3=denied, 5=roaming
    mode: int

    @property
    def registered(self) -> bool:
        return self.status in (1, 5)

    @property
    def roaming(self) -> bool:
        return self.status == 5

    @property
    def description(self) -> str:
        return {
            0: "not registered",
            1: "registered (home)",
            2: "searching",
            3: "registration denied",
            4: "unknown",
            5: "registered (roaming)",
        }.get(self.status, f"unknown ({self.status})")


class NetworkService:
    """Queries modem for network-related information."""

    def __init__(self, executor: ATCommandExecutor, bus: EventBus):
        self._at = executor
        self._bus = bus

    async def signal_quality(self) -> SignalInfo:
        """Query current signal quality (AT+CSQ).

        +CSQ is not a URC prefix, so the response comes back in resp.lines.
        """
        resp = await self._at.execute(ATCommand.SIGNAL_QUALITY, timeout=5.0)
        for line in resp.lines:
            parsed = ATResponseParser.parse_signal_quality(line)
            if parsed:
                rssi, ber = parsed
                info = SignalInfo(
                    rssi=rssi,
                    ber=ber,
                    dbm=rssi_to_dbm(rssi),
                    description=rssi_to_description(rssi),
                )
                await self._bus.emit(SignalQualityEvent(rssi=rssi, ber=ber))
                return info
        return SignalInfo(rssi=99, ber=99, dbm=None, description="unknown")

    async def registration(self) -> RegistrationInfo:
        """Query network registration status (AT+CREG?).

        +CREG is a URC prefix, so the executor dispatches the response line
        as a URC rather than including it in resp.lines. We capture it via
        the executor's capture_urcs context manager.
        """
        with self._at.capture_urcs("+CREG:", "+CGREG:") as cap:
            await self._at.execute(ATCommand.REGISTRATION, timeout=5.0)

        for line in cap.lines:
            parsed = ATResponseParser.parse_registration(line)
            if parsed:
                mode, status = parsed
                return RegistrationInfo(status=status, mode=mode)
        return RegistrationInfo(status=0, mode=0)

    async def operator(self) -> Optional[str]:
        """Query current network operator name (AT+COPS?)."""
        resp = await self._at.execute(ATCommand.OPERATOR, timeout=5.0)
        for line in resp.lines:
            m = _COPS_RE.match(line)
            if m:
                return m.group(1)
        return None

    async def wait_for_registration(self, timeout: float = 60.0, poll_interval: float = 2.0) -> bool:
        """Poll until the modem registers on a network.

        Returns True if registered, False on timeout.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            info = await self.registration()
            if info.registered:
                logger.info("Network registered: %s", info.description)
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(poll_interval, remaining))
        logger.warning("Network registration timeout after %.0fs", timeout)
        return False
