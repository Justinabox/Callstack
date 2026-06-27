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
FINAL_ERROR = ("ERROR",)
FINAL_ERROR_PREFIXES = ("+CME ERROR:", "+CMS ERROR:")
FINAL_DIAL_FAILURE = (
    "BUSY",
    "NO CARRIER",
    "NO ANSWER",
    "NO DIALTONE",
    "NO DIAL TONE",
)

# Sentinel pushed into the line queue when the transport dies
_TRANSPORT_ERROR = object()


def _decode_transport_line(raw: bytes) -> str:
    """Decode one transport-framed line without stripping message content."""
    return raw.decode("ascii", errors="replace").rstrip("\r\n")


def _redact_command_for_log(command: str) -> str:
    """Redact private payloads embedded in sensitive AT command/log lines."""
    stripped = command.lstrip()
    leading = command[: len(command) - len(stripped)]
    upper = stripped.upper()
    if upper.startswith("AT+CUSD="):
        return f"{leading}AT+CUSD=<redacted>"
    if upper.startswith("+CUSD:"):
        return f"{leading}+CUSD: <redacted>"
    return command


def _normalize_control_line(line: str) -> str:
    """Normalize AT control/header lines while keeping payload handling separate."""
    return line.strip()


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
        final_line = self.lines[-1]
        if (
            final_line in FINAL_OK
            or final_line in FINAL_ERROR
            or final_line in FINAL_DIAL_FAILURE
            or any(final_line.startswith(e) for e in FINAL_ERROR_PREFIXES)
        ):
            return self.lines[:-1]
        return list(self.lines)


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
                    if raw == b"":
                        raise TransportError("Transport closed (EOF)")
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
                raw_line = _decode_transport_line(raw)

                if self._command_in_flight:
                    await self._line_queue.put(raw_line)
                    continue

                control_line = _normalize_control_line(raw_line)
                if not control_line:
                    continue

                # Idle — dispatch URCs using normalized control headers while
                # preserving any follow-up payload body as framed.
                if self._urc.is_urc(control_line):
                    followup = ""
                    if self._urc.needs_followup(control_line):
                        try:
                            raw_f = await asyncio.wait_for(
                                self._transport.readline(), timeout=2.0
                            )
                            followup = _decode_transport_line(raw_f)
                        except asyncio.TimeoutError:
                            pass
                    await self._urc.dispatch(control_line, followup)
                else:
                    logger.debug("Ignoring non-URC idle line: %s", control_line)
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
                logger.debug("TX: %s", _redact_command_for_log(command))
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
                if raw == b"":
                    raise TransportError("Transport closed (EOF)")
            except asyncio.TimeoutError:
                raise
            except (TransportError, OSError) as exc:
                raise TransportError(
                    f"Transport error during command: {exc}"
                ) from exc
            line = _decode_transport_line(raw)
            return line

    def _command_preserves_urc_like_payload(self, command: str) -> bool:
        """Return true for commands whose data payload may look like URCs."""
        return command.startswith(("AT+CMGR", "AT+CMGL"))

    def _response_line_for_command(
        self, command: str, raw_line: str, control_line: str
    ) -> str:
        """Choose the response line representation exposed to callers.

        General AT control/header lines keep the historical stripped behavior.
        SMS read/list payload lines keep leading/trailing body whitespace.
        """
        if command.startswith("AT+CMGR"):
            if control_line.startswith("+CMGR:"):
                return control_line
            return raw_line
        if command.startswith("AT+CMGL"):
            if control_line.startswith("+CMGL:"):
                return control_line
            return raw_line
        return control_line

    def _line_matches_success(
        self,
        command: str,
        raw_line: str,
        control_line: str,
        expect: list[str] | tuple[str, ...],
    ) -> bool:
        """Return true when a line is an expected command terminator."""
        if self._command_preserves_urc_like_payload(command):
            return raw_line in expect
        return control_line in expect

    def _line_matches_error(
        self, command: str, raw_line: str, control_line: str
    ) -> bool:
        """Return true when a line is a final error result."""
        if self._command_preserves_urc_like_payload(command):
            line = raw_line
        else:
            line = control_line
        return line in FINAL_ERROR or any(line.startswith(e) for e in FINAL_ERROR_PREFIXES)

    def _line_matches_dial_failure(self, command: str, control_line: str) -> bool:
        """Return true for terminal voice-call results that fail ATD promptly."""
        return command.startswith("ATD") and control_line in FINAL_DIAL_FAILURE

    async def _collect_response(
        self, command: str, expect: list[str] | tuple[str, ...], timeout: float
    ) -> ATResponse:
        lines: list[str] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                safe_command = _redact_command_for_log(command)
                raise ATTimeoutError(
                    f"Timeout after {timeout}s waiting for {expect} (command: {safe_command})"
                )

            try:
                raw_line = await self._next_line(remaining)
            except asyncio.TimeoutError:
                safe_command = _redact_command_for_log(command)
                raise ATTimeoutError(
                    f"Timeout after {timeout}s waiting for {expect} (command: {safe_command})"
                )

            control_line = _normalize_control_line(raw_line)
            if not control_line and not self._command_preserves_urc_like_payload(command):
                continue

            logger.debug("RX: %s", _redact_command_for_log(raw_line))

            # Echo suppression: skip if the line matches the command we sent.
            if control_line == command:
                continue

            # During outbound dial, these modem call-progress results are the
            # terminal command result, not unsolicited remote-hangup URCs.  When
            # they arrive while idle they are still handled by the reader loop as
            # URCs outside command execution.
            if self._line_matches_dial_failure(command, control_line):
                lines.append(
                    self._response_line_for_command(command, raw_line, control_line)
                )
                return ATResponse(success=False, lines=lines)

            # Check if this is a URC that arrived during command execution.
            # SMS read/list responses can legitimately contain user text that
            # starts with URC prefixes, so keep those lines framed by the
            # command response's final result code instead of dispatching them.
            if (
                self._urc.is_urc(control_line)
                and not self._command_preserves_urc_like_payload(command)
            ):
                followup = ""
                if self._urc.needs_followup(control_line):
                    try:
                        followup_remaining = deadline - loop.time()
                        followup = await self._next_line(
                            max(followup_remaining, 0.5)
                        )
                    except asyncio.TimeoutError:
                        pass
                await self._urc.dispatch(control_line, followup)
                continue

            lines.append(
                self._response_line_for_command(command, raw_line, control_line)
            )

            # Check for expected success result codes. For SMS read/list commands,
            # do not trim body lines such as "OK "; they are payload until an
            # exact final result code frames the response. SMS send prompts still
            # match via normalized control lines because they are not CMGR/CMGL.
            if self._line_matches_success(command, raw_line, control_line, expect):
                return ATResponse(success=True, lines=lines)

            # Check for error result codes.
            if self._line_matches_error(command, raw_line, control_line):
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
