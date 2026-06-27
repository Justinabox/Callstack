"""PII-safe serialization for public typed events.

The monitor CLI and future realtime surfaces should expose bounded event metadata
without raw SMS bodies, USSD text, full phone numbers, raw AT lines, SIM
identifiers, or transport exception details.
"""

from __future__ import annotations

from datetime import timezone
from typing import Any

from callstack.events.types import (
    CallerIDEvent,
    CallStateEvent,
    DTMFEvent,
    Event,
    IncomingSMSEvent,
    ModemDisconnectedEvent,
    ModemReconnectedEvent,
    RingEvent,
    SMSDeliveryReportEvent,
    SMSSentEvent,
    SignalQualityEvent,
    USSDResponseEvent,
)
from callstack.privacy import redact_phone_number

EventEnvelope = dict[str, Any]
_DELIVERY_STATUSES = frozenset({"delivered", "failed", "pending"})


def _timestamp_for_event(event: Event) -> str:
    timestamp = event.timestamp
    if timestamp.tzinfo is not None:
        timestamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
    return f"{timestamp.isoformat()}Z"


def _envelope(event: Event, event_type: str, data: dict[str, Any]) -> EventEnvelope:
    return {
        "type": event_type,
        "timestamp": _timestamp_for_event(event),
        "data": data,
    }


def serialize_event(event: Event) -> EventEnvelope:
    """Return a JSON-safe, privacy-preserving envelope for a typed event."""
    if isinstance(event, IncomingSMSEvent):
        return _envelope(
            event,
            "sms.received",
            {
                "sender": redact_phone_number(event.sender),
                "body": "[redacted]",
                "body_length": len(event.body),
            },
        )

    if isinstance(event, SMSDeliveryReportEvent):
        status = event.status if event.status in _DELIVERY_STATUSES else "unknown"
        return _envelope(
            event,
            "sms.delivery_report",
            {
                "reference": event.reference,
                "recipient": redact_phone_number(event.recipient),
                "status": status,
            },
        )

    if isinstance(event, SMSSentEvent):
        return _envelope(
            event,
            "sms.sent",
            {
                "recipient": redact_phone_number(event.recipient),
                "reference": event.reference,
            },
        )

    if isinstance(event, CallStateEvent):
        return _envelope(event, "call.state", {"state": event.state.name.lower()})

    if isinstance(event, RingEvent):
        return _envelope(event, "call.ring", {})

    if isinstance(event, CallerIDEvent):
        return _envelope(
            event,
            "call.caller_id",
            {"number": redact_phone_number(event.number)},
        )

    if isinstance(event, DTMFEvent):
        return _envelope(event, "call.dtmf", {"digit": "[redacted]"})

    if isinstance(event, ModemDisconnectedEvent):
        return _envelope(event, "modem.state", {"connected": False})

    if isinstance(event, ModemReconnectedEvent):
        return _envelope(event, "modem.state", {"connected": True})

    if isinstance(event, SignalQualityEvent):
        return _envelope(
            event,
            "signal.quality",
            {"rssi": event.rssi, "ber": event.ber},
        )

    if isinstance(event, USSDResponseEvent):
        return _envelope(
            event,
            "ussd.response",
            {
                "status": event.status,
                "encoding": event.encoding,
                "message": "[redacted]",
                "message_length": len(event.message),
            },
        )

    raise ValueError(f"unsupported event type: {type(event).__name__}")
