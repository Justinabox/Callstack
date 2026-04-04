"""USSD service: send commands and receive responses."""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, Optional

from callstack.events.bus import EventBus
from callstack.events.types import USSDResponseEvent
from callstack.protocol.commands import ATCommand
from callstack.protocol.executor import ATCommandExecutor

logger = logging.getLogger("callstack.ussd")


class USSDService:
    """Send USSD commands and receive responses.

    USSD (Unstructured Supplementary Service Data) is used for balance
    checks, carrier menus, short codes, and prepaid plan management.

    Usage:
        # Simple query
        response = await modem.ussd.send("*100#")
        print(response.message)

        # Subscribe to network-initiated USSD
        modem.ussd.on_response(my_handler)
    """

    def __init__(self, executor: ATCommandExecutor, bus: EventBus):
        self._at = executor
        self._bus = bus

    async def send(self, code: str, timeout: float = 15.0) -> USSDResponseEvent:
        """Send a USSD command and wait for the response.

        Args:
            code: USSD code (e.g. "*100#", "*#06#").
            timeout: Seconds to wait for the network response.

        Returns:
            USSDResponseEvent with status, message, and encoding.
        """
        response_future: asyncio.Future[USSDResponseEvent] = asyncio.get_running_loop().create_future()

        async def capture(event: USSDResponseEvent) -> None:
            if not response_future.done():
                response_future.set_result(event)

        self._bus.subscribe(USSDResponseEvent, capture)
        try:
            resp = await self._at.execute(
                ATCommand.ussd_send(code), expect=["OK"], timeout=10.0
            )
            if not resp.success:
                raise RuntimeError(f"USSD command failed: {resp.lines}")

            return await asyncio.wait_for(response_future, timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"No USSD response within {timeout}s for {code}")
        finally:
            self._bus.unsubscribe(USSDResponseEvent, capture)

    async def cancel(self) -> None:
        """Cancel an ongoing USSD session."""
        await self._at.execute(ATCommand.USSD_CANCEL, expect=["OK"], timeout=5.0)

    def on_response(
        self, handler: Callable[[USSDResponseEvent], Awaitable[None]]
    ) -> Callable[[USSDResponseEvent], Awaitable[None]]:
        """Subscribe to USSD responses (including network-initiated)."""
        self._bus.subscribe(USSDResponseEvent, handler)
        return handler

    @asynccontextmanager
    async def responses(self):
        """Async iterator for USSD responses.

        Usage:
            async with modem.ussd.responses() as stream:
                async for event in stream:
                    print(event.message)
        """
        async with self._bus.stream(USSDResponseEvent) as stream:
            yield stream
