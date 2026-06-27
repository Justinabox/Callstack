"""USSD service: send commands and receive responses."""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, Optional

from callstack.errors import ATTimeoutError
from callstack.events.bus import EventBus
from callstack.events.types import USSDResponseEvent
from callstack.protocol.commands import ATCommand
from callstack.protocol.executor import ATCommandExecutor

logger = logging.getLogger("callstack.ussd")
_USSD_REQUIRES_RESET_ATTR = "_callstack_ussd_requires_reset"
_USSD_SEND_LOCK_ATTR = "_callstack_ussd_send_lock"


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

    def __init__(
        self,
        executor: ATCommandExecutor,
        bus: EventBus,
        command_timeout: float = 5.0,
    ):
        self._at = executor
        self._bus = bus
        self._command_timeout = command_timeout
        shared_lock = getattr(bus, _USSD_SEND_LOCK_ATTR, None)
        if shared_lock is None:
            shared_lock = asyncio.Lock()
            setattr(bus, _USSD_SEND_LOCK_ATTR, shared_lock)
        self._send_lock = shared_lock
        self._requires_cancel_after_timeout = False

    def _requires_session_reset(self) -> bool:
        return self._requires_cancel_after_timeout or bool(
            getattr(self._bus, _USSD_REQUIRES_RESET_ATTR, False)
        )

    def _mark_session_requires_reset(self) -> None:
        self._requires_cancel_after_timeout = True
        setattr(self._bus, _USSD_REQUIRES_RESET_ATTR, True)

    async def send(self, code: str, timeout: float = 15.0) -> USSDResponseEvent:
        """Send a USSD command and wait for the response.

        Args:
            code: USSD code (e.g. "*100#", "*#06#").
            timeout: Seconds to wait for the network response.

        Returns:
            USSDResponseEvent with status, message, and encoding.
        """
        async with self._send_lock:
            if self._requires_session_reset():
                raise RuntimeError(
                    "Previous USSD request did not complete; reset the modem session before sending another request"
                )

            response_future: asyncio.Future[USSDResponseEvent] = asyncio.get_running_loop().create_future()

            async def capture(event: USSDResponseEvent) -> None:
                if not response_future.done():
                    response_future.set_result(event)

            self._bus.subscribe(USSDResponseEvent, capture)
            try:
                resp = await self._at.execute(
                    ATCommand.ussd_send(code), expect=["OK"], timeout=self._command_timeout
                )
                if not resp.success:
                    raise RuntimeError(f"USSD command failed: {resp.lines}")

                return await asyncio.wait_for(response_future, timeout=timeout)
            except ATTimeoutError:
                self._mark_session_requires_reset()
                raise ATTimeoutError("USSD command timed out") from None
            except asyncio.TimeoutError:
                self._mark_session_requires_reset()
                raise TimeoutError(f"No USSD response within {timeout}s")
            except asyncio.CancelledError:
                self._mark_session_requires_reset()
                raise
            finally:
                self._bus.unsubscribe(USSDResponseEvent, capture)

    async def cancel(self) -> None:
        """Cancel an ongoing USSD session."""
        self._mark_session_requires_reset()
        await self._at.execute(
            ATCommand.USSD_CANCEL, expect=["OK"], timeout=self._command_timeout
        )

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
