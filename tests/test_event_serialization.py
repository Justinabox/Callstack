"""PII-safe event serialization helpers for local monitoring/realtime surfaces."""

import json
from datetime import datetime, timedelta, timezone

import pytest

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
from callstack.events.serialize import format_event_human, serialize_event


TS = datetime(2026, 6, 27, 12, 0, 0, tzinfo=timezone.utc)


def _assert_json_does_not_leak_private_values(payload, *private_values: str) -> None:
    rendered = json.dumps(payload, sort_keys=True)
    for value in private_values:
        assert value not in rendered


def test_default_event_timestamp_serializes_as_current_utc_instant():
    before = datetime.now(timezone.utc)
    event = IncomingSMSEvent(sender="+155****4567", body="secret")
    after = datetime.now(timezone.utc)

    assert event.timestamp.tzinfo is not None
    event_instant = event.timestamp.astimezone(timezone.utc)
    assert before - timedelta(seconds=1) <= event_instant <= after + timedelta(seconds=1)

    payload = serialize_event(event)

    serialized = datetime.fromisoformat(payload["timestamp"].removesuffix("Z")).replace(tzinfo=timezone.utc)
    assert before - timedelta(seconds=1) <= serialized <= after + timedelta(seconds=1)
    assert payload["timestamp"].endswith("Z")


def test_naive_event_timestamp_is_rejected_instead_of_labeled_utc():
    event = IncomingSMSEvent(
        timestamp=datetime(2026, 6, 27, 12, 0, 0),
        sender="+155****4567",
        body="secret",
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        serialize_event(event)


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
            message="Balance for +155****4567 is $12.34",
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
        "+155****4567",
        "$12.34",
    )


def test_incoming_sms_event_formats_as_human_line_without_body_or_full_sender():
    event = IncomingSMSEvent(
        timestamp=TS,
        sender="+155****4567",
        body="secret MFA code 123456",
        raw='+CMT: "+155****4567"\r\nsecret MFA code 123456',
    )

    line = format_event_human(event)

    assert line == "[12:00:00] sms received from +***4567 body_length=22"
    assert "secret MFA" not in line
    assert "+155****4567" not in line
    assert "+CMT" not in line


def test_sms_sent_event_formats_as_human_line_without_full_recipient():
    line = format_event_human(
        SMSSentEvent(
            timestamp=TS,
            recipient="+155****4321",
            reference=99,
        )
    )

    assert line == "[12:00:00] sms sent ref=99 recipient=+***4321"
    assert "+155****4321" not in line


def test_delivery_report_and_ussd_events_format_as_human_lines_without_private_content():
    report_line = format_event_human(
        SMSDeliveryReportEvent(
            timestamp=TS,
            reference=42,
            recipient="+155****6543",
            status="failed for +155****0000 raw +CMS ERROR: 500",
        )
    )
    ussd_line = format_event_human(
        USSDResponseEvent(
            timestamp=TS,
            status=0,
            message="Balance for +155****4567 is $12.34",
            encoding=15,
        )
    )

    assert report_line == "[12:00:00] sms delivery report ref=42 recipient=+***6543 status=unknown"
    assert ussd_line == "[12:00:00] ussd response status=0 encoding=15 message_length=34"
    combined = f"{report_line}\n{ussd_line}"
    for private_value in ("+155****6543", "+155****0000", "+CMS ERROR", "Balance", "+155****4567", "$12.34"):
        assert private_value not in combined


def test_call_modem_and_signal_events_format_as_bounded_human_lines():
    assert format_event_human(CallStateEvent(timestamp=TS, state=CallState.ACTIVE)) == "[12:00:00] call state active"
    assert format_event_human(RingEvent(timestamp=TS)) == "[12:00:00] call ring"
    caller_line = format_event_human(CallerIDEvent(timestamp=TS, number="+155****7890"))
    assert caller_line == "[12:00:00] caller id +***7890"
    assert "+155****7890" not in caller_line
    dtmf_line = format_event_human(DTMFEvent(timestamp=TS, digit="#"))
    assert dtmf_line == "[12:00:00] dtmf [redacted]"
    assert "#" not in dtmf_line
    assert format_event_human(ModemDisconnectedEvent(timestamp=TS, reason="USB EOF for +155****0000")) == "[12:00:00] modem disconnected"
    assert format_event_human(ModemReconnectedEvent(timestamp=TS)) == "[12:00:00] modem connected"
    assert format_event_human(SignalQualityEvent(timestamp=TS, rssi=18, ber=2)) == "[12:00:00] signal quality rssi=18 ber=2"
