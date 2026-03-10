"""Full SMS service: send, receive, subscribe, manage."""

import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Awaitable, Callable, Optional

from callstack.errors import SMSSendError, SMSReadError
from callstack.events.bus import EventBus, EventStream
from callstack.events.types import IncomingSMSEvent, SMSSentEvent, _RawSMSNotification
from callstack.protocol.commands import ATCommand
from callstack.protocol.executor import ATCommandExecutor
from callstack.protocol.parser import ATResponseParser
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
    ):
        self._at = executor
        self._bus = bus
        self._store = store or SMSStore()
        self._initialized = False
        self._pending_cmt_header: Optional[str] = None

        # Wire raw SMS URC notifications from URC dispatcher
        bus.subscribe(_RawSMSNotification, self._on_incoming)

    async def initialize(self) -> None:
        """Configure modem for SMS operations."""
        await self._store.initialize()

        # Text mode
        await self._at.execute(ATCommand.SMS_TEXT_MODE)
        # GSM charset
        await self._at.execute(ATCommand.SMS_CHARSET_GSM)
        # Route new SMS directly to TE (+CMT URCs)
        await self._at.execute(ATCommand.SMS_NOTIFY)
        # Enable delivery status reports
        await self._at.execute(ATCommand.SMS_DELIVERY_REPORT)

        self._initialized = True
        logger.info("SMS service initialized")

    # -- Sending --

    async def send(self, to: str, body: str) -> SMS:
        """Send an SMS message.

        Returns SMS with reference number on success.
        Raises SMSSendError on failure.
        """
        # Initiate SMS send - wait for ">" prompt
        resp = await self._at.execute(
            ATCommand.send_sms(to), expect=[">"], timeout=10
        )
        if not resp.success:
            raise SMSSendError(f"Failed to initiate SMS to {to}: {resp.lines}")

        # Send body + Ctrl+Z (0x1A) as raw bytes (no \r\n wrapping)
        resp = await self._at.send_data(
            f"{body}\x1A".encode("ascii", errors="replace"),
            expect=["+CMGS:", "OK"],
            timeout=30,
        )
        if not resp.success:
            raise SMSSendError(f"Failed to send SMS to {to}: {resp.lines}")

        # Extract reference number
        reference = 0
        for line in resp.lines:
            ref = ATResponseParser.parse_cmgs(line)
            if ref is not None:
                reference = ref
                break

        sms = SMS(
            recipient=to,
            body=body,
            status="sent",
            reference=reference,
            timestamp=datetime.now(),
        )
        await self._store.save(sms)
        await self._bus.emit(SMSSentEvent(recipient=to, reference=reference))
        logger.info("SMS sent to %s (ref: %d)", to, reference)
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
                    # Delete from SIM storage to prevent +SMS FULL
                    await self.delete_message(index)
                    sms.status = "unread"
                    await self._store.save(sms)
                    await self._bus.emit(
                        IncomingSMSEvent(sender=sms.sender, body=sms.body)
                    )
                    logger.info("Incoming SMS from %s (index %d)", sms.sender, index)

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
            logger.info("Incoming SMS from %s (direct)", sender)

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
        resp = await self._at.execute(ATCommand.list_sms(status), expect=["OK"], timeout=10)
        if not resp.success:
            return []
        return self._parse_message_list(resp.lines)

    async def read_message(self, index: int) -> Optional[SMS]:
        """Read a single message from SIM storage by index."""
        resp = await self._at.execute(ATCommand.read_sms(index), expect=["OK"], timeout=5)
        if not resp.success:
            return None
        return self._parse_single_message(resp.lines, index)

    async def delete_message(self, index: int) -> bool:
        """Delete a message from SIM storage by index."""
        resp = await self._at.execute(ATCommand.delete_sms(index), expect=["OK"])
        if resp.success:
            logger.debug("Deleted SIM message at index %d", index)
        return resp.success

    async def delete_all(self) -> bool:
        """Delete all messages from SIM storage."""
        resp = await self._at.execute(ATCommand.DELETE_ALL_SMS, expect=["OK"])
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

                # Next line is the message body
                body = ""
                if i + 1 < len(lines) and lines[i + 1] != "OK":
                    body = lines[i + 1]
                    i += 1

                messages.append(SMS(
                    sender=sender,
                    body=body,
                    status=status,
                    timestamp=timestamp,
                    storage_index=index,
                ))
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

                body = ""
                if i + 1 < len(lines) and lines[i + 1] != "OK":
                    body = lines[i + 1]

                return SMS(
                    sender=sender,
                    body=body,
                    status=status,
                    timestamp=timestamp,
                    storage_index=index,
                )
        return None


def _parse_timestamp(ts_str: str) -> Optional[datetime]:
    """Parse modem timestamp string (e.g. '24/12/25,14:30:00+04')."""
    if not ts_str:
        return None
    # Strip timezone offset for parsing
    ts_clean = re.sub(r'[+-]\d{1,2}$', '', ts_str)
    for fmt in ("%y/%m/%d,%H:%M:%S", "%Y/%m/%d,%H:%M:%S"):
        try:
            return datetime.strptime(ts_clean, fmt)
        except ValueError:
            continue
    return None
