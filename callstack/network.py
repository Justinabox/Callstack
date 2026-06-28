"""Network service: signal quality, registration status, operator info."""

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Optional

from callstack.errors import ATTimeoutError
from callstack.events.bus import EventBus
from callstack.events.types import SignalQualityEvent
from callstack.protocol.commands import ATCommand
from callstack.protocol.executor import ATCommandExecutor
from callstack.protocol.parser import ATResponseParser
from callstack.utils.signal_quality import ber_to_description, rssi_to_dbm, rssi_to_description

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
    ber_description: str = "unknown"


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

    def __init__(
        self,
        executor: ATCommandExecutor,
        bus: EventBus,
        command_timeout: float = 5.0,
    ):
        self._at = executor
        self._bus = bus
        self._command_timeout = command_timeout

    async def signal_quality(self) -> SignalInfo:
        """Query current signal quality (AT+CSQ).

        +CSQ is not a URC prefix, so the response comes back in resp.lines.
        """
        resp = await self._at.execute(ATCommand.SIGNAL_QUALITY, timeout=self._command_timeout)
        for line in resp.lines:
            parsed = ATResponseParser.parse_signal_quality(line)
            if parsed:
                rssi, ber = parsed
                info = SignalInfo(
                    rssi=rssi,
                    ber=ber,
                    dbm=rssi_to_dbm(rssi),
                    description=rssi_to_description(rssi),
                    ber_description=ber_to_description(ber),
                )
                await self._bus.emit(SignalQualityEvent(rssi=rssi, ber=ber))
                return info
        return SignalInfo(
            rssi=99,
            ber=99,
            dbm=None,
            description="unknown",
            ber_description="unknown",
        )

    async def registration(self) -> RegistrationInfo:
        """Query network registration status (AT+CREG?/AT+CGREG?/AT+CEREG?).

        Registration response prefixes are URC prefixes, so the executor
        dispatches response lines as URCs rather than including them in
        resp.lines. We capture them via the executor's capture_urcs context
        manager.
        """
        with self._at.capture_urcs("+CREG:", "+CGREG:", "+CEREG:") as cap:
            for command in (
                ATCommand.REGISTRATION,
                ATCommand.PACKET_REGISTRATION,
                ATCommand.LTE_REGISTRATION,
            ):
                try:
                    await self._at.execute(command, timeout=self._command_timeout)
                except ATTimeoutError:
                    logger.debug("Network registration query timed out: %s", command)
                    if cap.lines:
                        break
                    continue

        parsed_infos: list[RegistrationInfo] = []
        for line in cap.lines:
            parsed = ATResponseParser.parse_registration(line)
            if parsed:
                mode, status = parsed
                info = RegistrationInfo(status=status, mode=mode)
                if info.registered:
                    return info
                parsed_infos.append(info)

        if parsed_infos:
            # Prefer the non-registered state that is most useful for operators.
            # Unrecognized parsed modem statuses are still more specific than
            # generic "unknown"/"not registered" responses.
            status_priority = {3: 5, 2: 4, 4: 2, 0: 1}
            return max(parsed_infos, key=lambda info: status_priority.get(info.status, 3))
        return RegistrationInfo(status=0, mode=0)

    async def operator(self) -> Optional[str]:
        """Query current network operator name (AT+COPS?)."""
        resp = await self._at.execute(ATCommand.OPERATOR, timeout=self._command_timeout)
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
