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
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("event timestamp must be timezone-aware")
    timestamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
    return f"{timestamp.isoformat()}Z"


def _human_time_for_event(event: Event) -> str:
    timestamp = event.timestamp
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("event timestamp must be timezone-aware")
    timestamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
    return timestamp.strftime("%H:%M:%S")


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


def format_event_human(event: Event) -> str:
    """Return a concise, privacy-preserving terminal line for a typed event."""

    envelope = serialize_event(event)
    timestamp = _human_time_for_event(event)
    data = envelope["data"]
    event_type = envelope["type"]

    if event_type == "sms.received":
        return f"[{timestamp}] sms received from {data['sender']} body_length={data['body_length']}"
    if event_type == "sms.delivery_report":
        return (
            f"[{timestamp}] sms delivery report ref={data['reference']} "
            f"recipient={data['recipient']} status={data['status']}"
        )
    if event_type == "sms.sent":
        return f"[{timestamp}] sms sent ref={data['reference']} recipient={data['recipient']}"
    if event_type == "call.state":
        return f"[{timestamp}] call state {data['state']}"
    if event_type == "call.ring":
        return f"[{timestamp}] call ring"
    if event_type == "call.caller_id":
        return f"[{timestamp}] caller id {data['number']}"
    if event_type == "call.dtmf":
        return f"[{timestamp}] dtmf {data['digit']}"
    if event_type == "modem.state":
        state = "connected" if data["connected"] else "disconnected"
        return f"[{timestamp}] modem {state}"
    if event_type == "signal.quality":
        return f"[{timestamp}] signal quality rssi={data['rssi']} ber={data['ber']}"
    if event_type == "ussd.response":
        return (
            f"[{timestamp}] ussd response status={data['status']} "
            f"encoding={data['encoding']} message_length={data['message_length']}"
        )

    raise ValueError(f"unsupported serialized event type: {event_type}")
