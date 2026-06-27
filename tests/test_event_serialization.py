"""PII-safe event serialization helpers for local monitoring/realtime surfaces."""

import json
from datetime import datetime, timezone

from callstack.events.types import (
    CallerIDEvent,
    CallState,
    CallStateEvent,
    DTMFEvent,
    IncomingSMSEvent,
    ModemDisconnectedEvent,
    ModemReconnectedEvent,
    RingEvent,
    SMSDeliveryReportEvent,
    SMSSentEvent,
    SignalQualityEvent,
    USSDResponseEvent,
)
from callstack.events.serialize import serialize_event


TS = datetime(2026, 6, 27, 12, 0, 0, tzinfo=timezone.utc)


def _assert_json_does_not_leak_private_values(payload, *private_values: str) -> None:
    rendered = json.dumps(payload, sort_keys=True)
    for value in private_values:
        assert value not in rendered


def test_incoming_sms_event_serializes_without_body_raw_or_full_sender():
    event = IncomingSMSEvent(
        timestamp=TS,
        sender="+15551234567",
        body="secret MFA code 123456",
        raw='+CMT: "+15551234567"\r\nsecret MFA code 123456',
    )

    payload = serialize_event(event)

    assert payload == {
        "type": "sms.received",
        "timestamp": "2026-06-27T12:00:00Z",
        "data": {
            "sender": "+***4567",
            "body": "[redacted]",
            "body_length": 22,
        },
    }
    _assert_json_does_not_leak_private_values(
        payload,
        "+15551234567",
        "secret MFA code 123456",
        "+CMT:",
    )


def test_delivery_report_event_serializes_reference_status_and_masked_recipient():
    event = SMSDeliveryReportEvent(
        timestamp=TS,
        reference=42,
        recipient="+155****6543",
        status="delivered",
    )

    payload = serialize_event(event)

    assert payload == {
        "type": "sms.delivery_report",
        "timestamp": "2026-06-27T12:00:00Z",
        "data": {
            "reference": 42,
            "recipient": "+***6543",
            "status": "delivered",
        },
    }
    _assert_json_does_not_leak_private_values(payload, "+155****6543")


def test_delivery_report_event_replaces_unbounded_status_with_unknown():
    event = SMSDeliveryReportEvent(
        timestamp=TS,
        reference=43,
        recipient="+155****6543",
        status="failed for +155****0000 raw +CMS ERROR: 500",
    )

    payload = serialize_event(event)

    assert payload == {
        "type": "sms.delivery_report",
        "timestamp": "2026-06-27T12:00:00Z",
        "data": {
            "reference": 43,
            "recipient": "+***6543",
            "status": "unknown",
        },
    }
    _assert_json_does_not_leak_private_values(
        payload,
        "+155****6543",
        "+155****0000",
        "+CMS ERROR",
    )


def test_sms_sent_event_serializes_reference_and_masked_recipient():
    event = SMSSentEvent(
        timestamp=TS,
        recipient="+155****4321",
        reference=99,
    )

    payload = serialize_event(event)

    assert payload == {
        "type": "sms.sent",
        "timestamp": "2026-06-27T12:00:00Z",
        "data": {
            "recipient": "+***4321",
            "reference": 99,
        },
    }
    _assert_json_does_not_leak_private_values(payload, "+155****4321")


def test_call_and_modem_state_events_serialize_as_bounded_status_payloads():
    assert serialize_event(CallStateEvent(timestamp=TS, state=CallState.ACTIVE)) == {
        "type": "call.state",
        "timestamp": "2026-06-27T12:00:00Z",
        "data": {"state": "active"},
    }
    assert serialize_event(RingEvent(timestamp=TS)) == {
        "type": "call.ring",
        "timestamp": "2026-06-27T12:00:00Z",
        "data": {},
    }
    caller_payload = serialize_event(CallerIDEvent(timestamp=TS, number="+155****7890"))
    assert caller_payload == {
        "type": "call.caller_id",
        "timestamp": "2026-06-27T12:00:00Z",
        "data": {"number": "+***7890"},
    }
    _assert_json_does_not_leak_private_values(caller_payload, "+155****7890")
    dtmf_payload = serialize_event(DTMFEvent(timestamp=TS, digit="#"))
    assert dtmf_payload == {
        "type": "call.dtmf",
        "timestamp": "2026-06-27T12:00:00Z",
        "data": {"digit": "[redacted]"},
    }
    _assert_json_does_not_leak_private_values(dtmf_payload, "#")
    assert serialize_event(ModemDisconnectedEvent(timestamp=TS, reason="USB EOF for +155****0000")) == {
        "type": "modem.state",
        "timestamp": "2026-06-27T12:00:00Z",
        "data": {"connected": False},
    }
    assert serialize_event(ModemReconnectedEvent(timestamp=TS)) == {
        "type": "modem.state",
        "timestamp": "2026-06-27T12:00:00Z",
        "data": {"connected": True},
    }


def test_signal_quality_and_ussd_events_serialize_without_ussd_text():
    signal_payload = serialize_event(SignalQualityEvent(timestamp=TS, rssi=18, ber=2))
    ussd_payload = serialize_event(
        USSDResponseEvent(
            timestamp=TS,
            status=0,
            message="Balance for +15551234567 is $12.34",
            encoding=15,
        )
    )

    assert signal_payload == {
        "type": "signal.quality",
        "timestamp": "2026-06-27T12:00:00Z",
        "data": {"rssi": 18, "ber": 2},
    }
    assert ussd_payload == {
        "type": "ussd.response",
        "timestamp": "2026-06-27T12:00:00Z",
        "data": {
            "status": 0,
            "encoding": 15,
            "message": "[redacted]",
            "message_length": 34,
        },
    }
    _assert_json_does_not_leak_private_values(
        ussd_payload,
        "Balance",
        "+15551234567",
        "$12.34",
    )
