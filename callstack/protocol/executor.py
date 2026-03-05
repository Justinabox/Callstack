"""AT command executor with response correlation and URC separation.

Owns a single reader loop that demuxes all transport lines: when a command
is in-flight, lines are routed to the response collector; otherwise they
are dispatched as URCs.
"""

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Optional

from callstack.errors import ATTimeoutError, TransportError
from callstack.transport.base import Transport
from callstack.protocol.urc import URCDispatcher

logger = logging.getLogger("callstack.executor")

# Final result codes that terminate a command response
FINAL_OK = ("OK",)
FINAL_ERROR = ("ERROR", "+CME ERROR", "+CMS ERROR")

# Sentinel pushed into the line queue when the transport dies
_TRANSPORT_ERROR = object()


@dataclass
class ATResponse:
    """Structured AT command response."""
    success: bool
    lines: list[str] = field(default_factory=list)

    @property
    def data_lines(self) -> list[str]:
        """Response lines excluding the final result code."""
        if not self.lines:
            return []
        return [l for l in self.lines if l not in FINAL_OK and not any(l.startswith(e) for e in FINAL_ERROR)]


class ATCommandExecutor:
    """Send AT commands and await structured responses.

    Owns a centralized reader loop so that only one task ever calls
    readline() on the transport.  When a command is in-flight, incoming
    lines are placed in an internal queue for _collect_response to consume.
    When idle, lines are checked for URC patterns and dispatched directly.
    """

    def __init__(self, transport: Transport, urc_dispatcher: URCDispatcher):
        self._transport = transport
        self._urc = urc_dispatcher
        self._lock = asyncio.Lock()
        # Reader loop state
        self._reader_task: Optional[asyncio.Task] = None
        self._line_queue: asyncio.Queue = asyncio.Queue()
        self._command_in_flight = False
        self._shutdown = asyncio.Event()
        self._transport_error: Optional[Exception] = None
        self._on_done_callback = None

    # -- Reader loop lifecycle --

    async def start_reader(self) -> None:
        """Start the background reader that owns all transport reads."""
        self._shutdown.clear()
        self._transport_error = None
        self._reader_task = asyncio.create_task(self._reader_loop())
        if self._on_done_callback:
            self._reader_task.add_done_callback(self._on_done_callback)

    async def stop_reader(self) -> None:
        """Stop the background reader."""
        self._shutdown.set()
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None

    def on_reader_done(self, callback) -> None:
        """Register a persistent callback for when the reader task completes.

        The callback is stored and automatically attached to new reader tasks
        created by start_reader(), so callers only need to register once.
        """
        self._on_done_callback = callback
        if self._reader_task:
            self._reader_task.add_done_callback(callback)

    _MAX_CONSECUTIVE_ERRORS = 5

    async def _reader_loop(self) -> None:
        """Read lines from transport and route them.

        When a command is in-flight → push to _line_queue.
        When idle → dispatch as URC (including multiline follow-up).
        """
        consecutive_errors = 0
        try:
            while not self._shutdown.is_set():
                try:
                    raw = await self._transport.readline()
                except asyncio.CancelledError:
                    break
                except (TransportError, OSError) as exc:
                    if self._shutdown.is_set():
                        break
                    logger.error("Transport error in reader: %s", exc)
                    self._transport_error = exc
                    # Wake up any waiting command
                    await self._line_queue.put(_TRANSPORT_ERROR)
                    raise
                except Exception as exc:
                    consecutive_errors += 1
                    logger.exception("Unexpected error in reader: %s", exc)
                    if consecutive_errors >= self._MAX_CONSECUTIVE_ERRORS:
                        logger.error("Too many consecutive reader errors, stopping reader")
                        raise
                    await asyncio.sleep(0.1)
                    continue

                consecutive_errors = 0
                line = raw.decode("ascii", errors="replace").strip()
                if not line:
                    continue

                if self._command_in_flight:
                    await self._line_queue.put(line)
                else:
                    # Idle — dispatch URCs
                    if self._urc.is_urc(line):
                        followup = ""
                        if self._urc.needs_followup(line):
                            try:
                                raw_f = await asyncio.wait_for(
                                    self._transport.readline(), timeout=2.0
                                )
                                followup = raw_f.decode("ascii", errors="replace").strip()
                            except asyncio.TimeoutError:
                                pass
                        await self._urc.dispatch(line, followup)
                    else:
                        logger.debug("Ignoring non-URC idle line: %s", line)
        finally:
            self._command_in_flight = False

    # -- URC capture --

    def capture_urcs(self, *prefixes: str) -> "URCCapture":
        """Create a context manager that captures URC lines matching given prefixes.

        Captured lines are still dispatched normally but also collected.

        Usage:
            with executor.capture_urcs("+CREG:", "+CGREG:") as captured:
                await executor.execute("AT+CREG?")
            # captured.lines contains ["+CREG: 0,1"] etc.
        """
        return URCCapture(self._urc, prefixes)

    # -- Command execution --

    @property
    def _reader_active(self) -> bool:
        return self._reader_task is not None and not self._reader_task.done()

    async def execute(
        self,
        command: str,
        expect: list[str] | tuple[str, ...] = ("OK",),
        timeout: float = 5.0,
    ) -> ATResponse:
        """Send an AT command and wait for a final result code.

        Returns ATResponse with success=True if one of the expected
        result codes is found, or success=False on ERROR.
        """
        async with self._lock:
            if self._reader_active:
                self._drain_queue()
                self._command_in_flight = True
            try:
                logger.debug("TX: %s", command)
                await self._transport.write(f"{command}\r\n".encode())
                return await self._collect_response(command, expect, timeout)
            finally:
                self._command_in_flight = False

    async def send_data(
        self,
        data: bytes,
        expect: list[str] | tuple[str, ...] = ("OK",),
        timeout: float = 30.0,
    ) -> ATResponse:
        """Send raw data bytes without \\r\\n wrapping and collect a response.

        Used for SMS body transmission where the payload must not be
        wrapped in line terminators.
        """
        async with self._lock:
            if self._reader_active:
                self._drain_queue()
                self._command_in_flight = True
            try:
                logger.debug("TX (raw): %d bytes", len(data))
                await self._transport.write(data)
                return await self._collect_response("<raw-data>", expect, timeout)
            finally:
                self._command_in_flight = False

    def _drain_queue(self) -> None:
        """Discard stale lines left in the queue from a prior command."""
        while not self._line_queue.empty():
            try:
                self._line_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _next_line(self, timeout: float) -> str:
        """Get the next response line, from queue or direct transport read."""
        if self._reader_active:
            item = await asyncio.wait_for(
                self._line_queue.get(), timeout=timeout
            )
            if item is _TRANSPORT_ERROR:
                raise TransportError(
                    f"Transport died during command: {self._transport_error}"
                )
            return item
        else:
            try:
                raw = await asyncio.wait_for(
                    self._transport.readline(), timeout=timeout
                )
            except (TransportError, OSError) as exc:
                raise TransportError(
                    f"Transport error during command: {exc}"
                ) from exc
            line = raw.decode("ascii", errors="replace").strip()
            return line

    async def _collect_response(
        self, command: str, expect: list[str] | tuple[str, ...], timeout: float
    ) -> ATResponse:
        lines: list[str] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise ATTimeoutError(
                    f"Timeout after {timeout}s waiting for {expect} (command: {command})"
                )

            try:
                line = await self._next_line(remaining)
            except asyncio.TimeoutError:
                raise ATTimeoutError(
                    f"Timeout after {timeout}s waiting for {expect} (command: {command})"
                )

            if not line:
                continue

            logger.debug("RX: %s", line)

            # Echo suppression: skip if the line matches the command we sent
            if line == command:
                continue

            # Check if this is a URC that arrived during command execution
            if self._urc.is_urc(line):
                followup = ""
                if self._urc.needs_followup(line):
                    try:
                        followup_remaining = deadline - loop.time()
                        followup = await self._next_line(
                            max(followup_remaining, 0.5)
                        )
                    except asyncio.TimeoutError:
                        pass
                await self._urc.dispatch(line, followup)
                continue

            lines.append(line)

            # Check for expected success result codes
            if any(e in line for e in expect):
                return ATResponse(success=True, lines=lines)

            # Check for error result codes
            if any(line.startswith(e) for e in FINAL_ERROR) or line == "ERROR":
                return ATResponse(success=False, lines=lines)


class URCCapture:
    """Context manager that captures URC lines matching specific prefixes.

    Lines are still dispatched normally via the URCDispatcher but also
    collected in self.lines for the caller to inspect after the command.
    """

    def __init__(self, urc: URCDispatcher, prefixes: tuple[str, ...]):
        self._urc = urc
        self._prefixes = prefixes
        self.lines: list[str] = []

    def __enter__(self) -> "URCCapture":
        self._urc.add_capture_hook(self._prefixes, self.lines)
        return self

    def __exit__(self, *exc) -> None:
        self._urc.remove_capture_hook(self._prefixes, self.lines)
