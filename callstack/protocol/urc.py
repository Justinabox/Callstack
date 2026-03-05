"""Unsolicited Result Code dispatcher."""

import logging

from callstack.events.bus import EventBus
from callstack.events.types import (
    CallState,
    CallStateEvent,
    CallerIDEvent,
    DTMFEvent,
    ModemDisconnectedEvent,
    RingEvent,
    SignalQualityEvent,
    _RawSMSNotification,
)
from callstack.protocol.parser import ATResponseParser

logger = logging.getLogger("callstack.urc")


class URCDispatcher:
    """Routes unsolicited result codes to typed events on the EventBus."""

    URC_PREFIXES = (
        "RING", "+CLIP", "+DTMF", "RXDTMF",
        "+CMT", "+CMTI", "+CDSI",
        "VOICE CALL", "NO CARRIER", "BUSY", "NO ANSWER",
        "+CUSD", "+CREG", "+CGREG",
    )

    def __init__(self, event_bus: EventBus):
        self._bus = event_bus
        self._capture_hooks: list[tuple[tuple[str, ...], list[str]]] = []

    # URCs that are followed by a second line (e.g. +CMT: header then body)
    MULTILINE_PREFIXES = ("+CMT:",)

    def is_urc(self, line: str) -> bool:
        """Check if a response line is an unsolicited result code."""
        return any(line.startswith(p) for p in self.URC_PREFIXES)

    def needs_followup(self, line: str) -> bool:
        """Check if this URC expects a continuation line (e.g. +CMT body)."""
        return any(line.startswith(p) for p in self.MULTILINE_PREFIXES)

    def add_capture_hook(self, prefixes: tuple[str, ...], lines: list[str]) -> None:
        """Register a capture hook that collects URC lines matching prefixes."""
        self._capture_hooks.append((prefixes, lines))

    def remove_capture_hook(self, prefixes: tuple[str, ...], lines: list[str]) -> None:
        """Remove a previously registered capture hook."""
        try:
            self._capture_hooks.remove((prefixes, lines))
        except ValueError:
            pass

    async def dispatch(self, line: str, followup: str = "") -> None:
        """Parse a URC line and emit the corresponding typed event.

        Args:
            line: The URC line.
            followup: Optional second line for multi-line URCs (e.g. SMS body).
        """
        try:
            self._dispatch_to_capture_hooks(line)
            await self._dispatch_event(line, followup)
        except Exception as exc:
            logger.exception("Error dispatching URC '%s': %s", line, exc)

    def _dispatch_to_capture_hooks(self, line: str) -> None:
        """Check capture hooks and collect matching lines."""
        for prefixes, captured_lines in self._capture_hooks:
            if any(line.startswith(p) for p in prefixes):
                captured_lines.append(line)

    async def _dispatch_event(self, line: str, followup: str) -> None:
        """Route a URC line to the appropriate typed event."""
        logger.debug("URC: %s", line)

        if line == "RING":
            await self._bus.emit(RingEvent())

        elif line.startswith("+CLIP:"):
            number = ATResponseParser.parse_clip(line) or ""
            await self._bus.emit(CallerIDEvent(number=number))

        elif line.startswith("+DTMF:") or line.startswith("RXDTMF:"):
            parts = line.split(":", 1)
            digit = parts[1].strip() if len(parts) > 1 else ""
            if digit:
                await self._bus.emit(DTMFEvent(digit=digit))

        elif line == "VOICE CALL: BEGIN":
            await self._bus.emit(CallStateEvent(state=CallState.ACTIVE))

        elif line.startswith("VOICE CALL: END"):
            await self._bus.emit(CallStateEvent(state=CallState.ENDED))

        elif line == "NO CARRIER" or line == "BUSY" or line == "NO ANSWER":
            await self._bus.emit(CallStateEvent(state=CallState.ENDED))

        elif line.startswith("+CMT:"):
            sender = ATResponseParser.parse_cmt(line) or ""
            await self._bus.emit(_RawSMSNotification(sender=sender, body=followup, raw=line))

        elif line.startswith("+CMTI:"):
            await self._bus.emit(_RawSMSNotification(raw=line))

        elif line.startswith("+CDSI:"):
            logger.info("SMS delivery report: %s", line)

        elif line.startswith("+CREG:") or line.startswith("+CGREG:"):
            logger.info("Network registration: %s", line)

        else:
            logger.warning("Unhandled URC: %s", line)
