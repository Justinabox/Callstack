"""Full SMS service: send, receive, subscribe, manage."""

import csv
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

from callstack.errors import SMSSendError, SMSReadError
from callstack.events.bus import EventBus, EventStream
from callstack.events.types import (
    IncomingSMSEvent,
    SMSDeliveryReportEvent,
    SMSSentEvent,
    _RawDeliveryReport,
    _RawSMSNotification,
)
from callstack.protocol.commands import ATCommand
from callstack.protocol.executor import ATCommandExecutor
from callstack.protocol.parser import ATResponseParser
from callstack.privacy import redact_phone_number
from callstack.sms.pdu import GSM7_BASIC
from callstack.sms.store import SMSStore
from callstack.sms.types import SMS, SMSStatus

logger = logging.getLogger("callstack.sms")

# Pattern for +CMGL list entries: +CMGL: index,"status","sender","","timestamp"
_CMGL_RE = re.compile(
    r'^\+CMGL:\s*(\d+),"([^"]*)","([^"]*)","([^"]*)",?"?([^"]*)"?$'
)

# Pattern for +CMGR read: +CMGR: "status","sender","","timestamp"
_CMGR_RE = re.compile(
    r'^\+CMGR:\s*"([^"]*)","([^"]*)","([^"]*)",?"?([^"]*)"?$'
)

# Pattern for +CMGS send reference: +CMGS: ref
_CMGS_REF_RE = re.compile(r"^\+CMGS:\s*(\d+)")

_GSM_TEXT_RESERVED_CODES = {0x1A, 0x1B}
_GSM_TEXT_BASIC = {
    char: code
    for code, char in enumerate(GSM7_BASIC)
    if code not in _GSM_TEXT_RESERVED_CODES
}
_GSM_TEXT_EXTENDED = {
    "\f": 0x0A,
    "^": 0x14,
    "{": 0x28,
    "}": 0x29,
    "\\": 0x2F,
    "[": 0x3C,
    "~": 0x3D,
    "]": 0x3E,
    "|": 0x40,
    "€": 0x65,
}


class _FilteredStream:
    """Wraps an EventStream to filter incoming SMS events by sender."""

    def __init__(self, stream: EventStream, filter_sender: Optional[str]):
        self._stream = stream
        self._filter = filter_sender

    def __aiter__(self):
        return self

    async def __anext__(self) -> IncomingSMSEvent:
        while True:
            event = await self._stream.__anext__()
            if isinstance(event, IncomingSMSEvent):
                if self._filter is None or event.sender == self._filter:
                    return event


class SMSService:
    """Full SMS capabilities: send, receive, subscribe, manage.

    Operates in text mode (AT+CMGF=1) by default. Supports both direct
    delivery (+CMT) and notification-based (+CMTI) incoming SMS routing.
    """

    def __init__(
        self,
        executor: ATCommandExecutor,
        bus: EventBus,
        store: Optional[SMSStore] = None,
        command_timeout: float = 5.0,
        sms_prompt_timeout: float = 10.0,
        sms_submit_timeout: float = 30.0,
    ):
        self._at = executor
        self._bus = bus
        self._store = store or SMSStore()
        self._command_timeout = command_timeout
        self._sms_prompt_timeout = sms_prompt_timeout
        self._sms_submit_timeout = sms_submit_timeout
        self._initialized = False
        self._pending_cmt_header: Optional[str] = None

        # Wire raw SMS URC notifications from URC dispatcher
        bus.subscribe(_RawSMSNotification, self._on_incoming)
        bus.subscribe(_RawDeliveryReport, self._on_delivery_report)

    async def initialize(self) -> None:
        """Configure modem for SMS operations."""
        await self._store.initialize()

        # Text mode
        await self._at.execute(ATCommand.SMS_TEXT_MODE, timeout=self._command_timeout)
        # GSM charset
        await self._at.execute(ATCommand.SMS_CHARSET_GSM, timeout=self._command_timeout)
        # Route new SMS to SIM storage (+CMTI URCs), then fetch via +CMGR so
        # modem final result codes frame complete multiline message bodies.
        await self._at.execute(ATCommand.SMS_NOTIFY, timeout=self._command_timeout)
        # Enable delivery status reports
        await self._at.execute(ATCommand.SMS_DELIVERY_REPORT, timeout=self._command_timeout)

        self._initialized = True
        logger.info("SMS service initialized")

    # -- Sending --

    async def send(self, to: str, body: str) -> SMS:
        """Send an SMS message.

        Returns SMS with reference number on success.
        Raises SMSSendError on failure.
        """
        payload = bytearray()
        for char in body:
            if char in _GSM_TEXT_EXTENDED:
                payload.extend((0x1B, _GSM_TEXT_EXTENDED[char]))
            elif char in _GSM_TEXT_BASIC:
                payload.append(_GSM_TEXT_BASIC[char])
            else:
                raise SMSSendError(
                    "SMS body cannot be encoded with GSM 03.38 text mode; "
                    "UCS2/PDU sending is not implemented yet"
                )
        payload.append(0x1A)

        # Initiate SMS send - wait for ">" prompt
        resp = await self._at.execute(
            ATCommand.send_sms(to), expect=[">"], timeout=self._sms_prompt_timeout
        )
        if not resp.success:
            raise SMSSendError(f"Failed to initiate SMS to {to}: {resp.lines}")

        # Send body + Ctrl+Z (0x1A) as GSM 03.38 text-mode bytes.
        resp = await self._at.send_data(
            bytes(payload),
            expect=["+CMGS:", "OK"],
            timeout=self._sms_submit_timeout,
        )
        if not resp.success:
            raise SMSSendError(f"Failed to send SMS to {to}: {resp.lines}")

        # Extract the modem submit reference. A final OK confirms command
        # framing, but delivery-report correlation requires an explicit +CMGS.
        reference: int | None = None
        for line in resp.lines:
            ref = ATResponseParser.parse_cmgs(line)
            if ref is not None:
                reference = ref
                break
        if reference is None:
            raise SMSSendError("SMS submit response missing +CMGS reference")

        sms = SMS(
            recipient=to,
            body=body,
            status="sent",
            reference=reference,
            timestamp=datetime.now(),
        )
        await self._store.save(sms)
        await self._bus.emit(SMSSentEvent(recipient=to, reference=reference))
        logger.info("SMS sent to %s (ref: %d)", redact_phone_number(to), reference)
        return sms

    # -- Receiving --

    async def _on_incoming(self, event: _RawSMSNotification) -> None:
        """Handle raw SMS URC notification from the URC dispatcher.

        Processes raw URC data and emits enriched IncomingSMSEvent for
        user-facing subscribers.
        """
        raw = event.raw

        if raw.startswith("+CMTI:"):
            # Notification mode: fetch the message from storage
            parsed = ATResponseParser.parse_cmti(raw)
            if parsed:
                storage, index = parsed
                logger.debug("CMTI notification: storage=%s, index=%d", storage, index)
                sms = await self.read_message(index)
                if sms:
                    sms.status = "unread"
                    await self._store.save(sms)
                    # Delete from SIM storage after durable/local acceptance to prevent +SMS FULL
                    await self.delete_message(index)
                    await self._bus.emit(
                        IncomingSMSEvent(sender=sms.sender, body=sms.body)
                    )
                    logger.info("Incoming SMS from %s (index %d)", redact_phone_number(sms.sender), index)

        elif raw.startswith("+CMT:"):
            # Direct delivery mode: sender is in the header, body follows
            sender = event.sender or ATResponseParser.parse_cmt(raw) or "unknown"
            sms = SMS(
                sender=sender,
                body=event.body,
                status="unread",
                timestamp=datetime.now(),
            )
            await self._store.save(sms)
            await self._bus.emit(
                IncomingSMSEvent(sender=sender, body=event.body)
            )
            logger.info("Incoming SMS from %s (direct)", redact_phone_number(sender))

    # -- Delivery Reports --

    async def _on_delivery_report(self, event: _RawDeliveryReport) -> None:
        """Handle +CDSI delivery report notification."""
        logger.debug("Delivery report: storage=%s, index=%d", event.storage, event.index)
        resp = await self._at.execute(
            ATCommand.read_sms(event.index), expect=["OK"], timeout=self._command_timeout
        )
        if not resp.success:
            logger.warning("Failed to read delivery report at index %d", event.index)
            return

        # Parse the delivery status from response lines.
        # Text-mode status report format:
        # +CMGR: "REC READ",<mr>,"<recipient>",<tora>,"<scts>","<dt>",<st>
        reference = 0
        recipient = ""
        status = ""
        for line in resp.data_lines:
            if line.startswith("+CMGR:"):
                report = _parse_cmgr_status_report(line)
                if report is not None:
                    reference, recipient, status = report

        if not status:
            logger.warning(
                "Could not parse delivery report at index %d", event.index
            )
            return

        await self._at.execute(
            ATCommand.delete_sms(event.index),
            expect=["OK"],
            timeout=self._command_timeout,
        )

        await self._bus.emit(SMSDeliveryReportEvent(
            reference=reference,
            recipient=recipient,
            status=status,
        ))
        logger.info("Delivery report reference %d: %s", reference, status)

    # -- Subscription API --

    def on_message(
        self,
        handler: Callable[[IncomingSMSEvent], Awaitable[None]],
        filter_sender: Optional[str] = None,
    ) -> Callable[[IncomingSMSEvent], Awaitable[None]]:
        """Subscribe to incoming messages with optional sender filter.

        Returns the actual subscribed callable (needed for unsubscription
        when filter_sender wraps the original handler).

        Usage:
            sms_service.on_message(my_handler)
            sub = sms_service.on_message(my_handler, filter_sender="+1555...")
            # To unsubscribe later: bus.unsubscribe(IncomingSMSEvent, sub)
        """
        if filter_sender:
            async def filtered(event: IncomingSMSEvent) -> None:
                if event.sender == filter_sender:
                    await handler(event)
            self._bus.subscribe(IncomingSMSEvent, filtered)
            return filtered
        else:
            self._bus.subscribe(IncomingSMSEvent, handler)
            return handler

    @asynccontextmanager
    async def messages(self, filter_sender: Optional[str] = None):
        """Async iterator for incoming SMS messages.

        Usage:
            async with sms_service.messages() as inbox:
                async for msg in inbox:
                    print(f"From {msg.sender}: {msg.body}")
        """
        async with self._bus.stream(IncomingSMSEvent) as stream:
            yield _FilteredStream(stream, filter_sender)

    # -- Message Management --

    async def list_messages(self, status: str = "ALL") -> list[SMS]:
        """List messages stored on SIM.

        Status values: "ALL", "REC UNREAD", "REC READ", "STO UNSENT", "STO SENT"
        """
        resp = await self._at.execute(
            ATCommand.list_sms(status), expect=["OK"], timeout=self._command_timeout
        )
        if not resp.success:
            return []
        return self._parse_message_list(resp.lines)

    async def read_message(self, index: int) -> Optional[SMS]:
        """Read a single message from SIM storage by index."""
        resp = await self._at.execute(
            ATCommand.read_sms(index), expect=["OK"], timeout=self._command_timeout
        )
        if not resp.success:
            return None
        return self._parse_single_message(resp.lines, index)

    async def delete_message(self, index: int) -> bool:
        """Delete a message from SIM storage by index."""
        resp = await self._at.execute(
            ATCommand.delete_sms(index), expect=["OK"], timeout=self._command_timeout
        )
        if resp.success:
            logger.debug("Deleted SIM message at index %d", index)
        return resp.success

    async def delete_all(self) -> bool:
        """Delete all messages from SIM storage."""
        resp = await self._at.execute(
            ATCommand.DELETE_ALL_SMS, expect=["OK"], timeout=self._command_timeout
        )
        if resp.success:
            logger.info("Deleted all SIM messages")
        return resp.success

    # -- Parsing helpers --

    @staticmethod
    def _parse_message_list(lines: list[str]) -> list[SMS]:
        """Parse AT+CMGL response lines into SMS objects."""
        messages: list[SMS] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            m = _CMGL_RE.match(line)
            if m:
                index = int(m.group(1))
                status = m.group(2)
                sender = m.group(3)
                timestamp_str = m.group(5) if m.group(5) else m.group(4)
                timestamp = _parse_timestamp(timestamp_str)
                body, next_index = _collect_message_body(
                    lines,
                    i + 1,
                    stop_on_cmgl_header=True,
                )

                messages.append(SMS(
                    sender=sender,
                    body=body,
                    status=status,
                    timestamp=timestamp,
                    storage_index=index,
                ))
                i = next_index
                continue
            i += 1
        return messages

    @staticmethod
    def _parse_single_message(lines: list[str], index: int) -> Optional[SMS]:
        """Parse AT+CMGR response lines into an SMS object."""
        for i, line in enumerate(lines):
            m = _CMGR_RE.match(line)
            if m:
                status = m.group(1)
                sender = m.group(2)
                timestamp_str = m.group(4) if m.group(4) else m.group(3)
                timestamp = _parse_timestamp(timestamp_str)
                body, _ = _collect_message_body(
                    lines,
                    i + 1,
                    stop_on_cmgl_header=False,
                )

                return SMS(
                    sender=sender,
                    body=body,
                    status=status,
                    timestamp=timestamp,
                    storage_index=index,
                )
        return None


def _parse_cmgr_status_report(line: str) -> Optional[tuple[int, str, str]]:
    """Parse a text-mode +CMGR status-report header.

    Uses CSV parsing so quoted timestamps containing commas remain a single field.
    """
    if not line.startswith("+CMGR:"):
        return None

    payload = line.split(":", 1)[1].strip()
    try:
        fields = next(csv.reader([payload], skipinitialspace=True))
    except csv.Error:
        return None

    if len(fields) < 7:
        return None

    try:
        reference = int(fields[1].strip())
        status_code = int(fields[-1].strip())
    except ValueError:
        return None

    # TP-ST status grouping per 3GPP TS 23.040 section 9.2.3.15:
    # 0x00..0x1f completed, 0x20..0x3f temporary error/SC still trying,
    # and 0x40+ final failure or reserved ranges.
    if 0 <= status_code <= 0x1F:
        status = "delivered"
    elif 0x20 <= status_code <= 0x3F:
        status = "pending"
    else:
        status = "failed"

    return reference, fields[2].strip(), status


def _collect_message_body(
    lines: list[str],
    start: int,
    *,
    stop_on_cmgl_header: bool,
) -> tuple[str, int]:
    """Collect SMS body lines until a record boundary or final result code."""
    body_lines: list[str] = []
    i = start
    while i < len(lines):
        line = lines[i]
        if _is_final_result_code(line) or (stop_on_cmgl_header and _CMGL_RE.match(line)):
            break
        body_lines.append(line)
        i += 1
    return "\n".join(body_lines), i


def _is_final_result_code(line: str) -> bool:
    """Return True for AT final result lines that terminate SMS reads."""
    return line in {"OK", "ERROR"} or line.startswith(("+CME ERROR:", "+CMS ERROR:"))


def _parse_timestamp(ts_str: str) -> Optional[datetime]:
    """Parse modem timestamp string (e.g. '24/12/25,14:30:00+04')."""
    if not ts_str:
        return None

    tzinfo = None
    ts_clean = ts_str
    tz_match = re.search(r"([+-])(\d{1,2})$", ts_str)
    if tz_match:
        sign, quarters = tz_match.groups()
        offset_minutes = int(quarters) * 15
        if sign == "-":
            offset_minutes *= -1
        try:
            tzinfo = timezone(timedelta(minutes=offset_minutes))
        except ValueError:
            return None
        ts_clean = ts_str[: tz_match.start()]

    for fmt in ("%y/%m/%d,%H:%M:%S", "%Y/%m/%d,%H:%M:%S"):
        try:
            parsed = datetime.strptime(ts_clean, fmt)
            if tzinfo is not None:
                return parsed.replace(tzinfo=tzinfo)
            return parsed
        except ValueError:
            continue
    return None
