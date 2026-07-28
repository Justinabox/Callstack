"""Authenticated PII-safe WebSocket realtime endpoint tests."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

import server
from callstack.events.bus import EventBus
from callstack.events.types import (
    IncomingSMSEvent,
    ModemDisconnectedEvent,
    ModemReconnectedEvent,
    SMSDeliveryReportEvent,
    SignalQualityEvent,
)
from server import (
    SUPPORTED_WEBSOCKET_EVENTS,
    _enqueue_websocket_envelope,
    _resolve_websocket_queue_maxsize,
    create_app,
)


class _FakeModem:
    def __init__(self):
        self.connected = True
        self.bus = EventBus()
        self.sms = SimpleNamespace(send=AsyncMock(), store=object())
        self.ussd = SimpleNamespace(send=AsyncMock())


async def _receive_json(ws, timeout: float = 1.0):
    return await asyncio.wait_for(ws.receive_json(), timeout=timeout)


async def test_ws_requires_api_key_when_server_auth_is_enabled(aiohttp_client):
    client = await aiohttp_client(create_app(_FakeModem(), api_keys=["test-key"]))

    with pytest.raises(aiohttp.WSServerHandshakeError) as excinfo:
        await client.ws_connect("/ws")

    assert excinfo.value.status == 401


async def test_ws_sends_hello_with_supported_public_event_names(aiohttp_client):
    client = await aiohttp_client(create_app(_FakeModem(), api_keys=["test-key"]))

    ws = await client.ws_connect("/ws", headers={"Authorization": "Bearer test-key"})
    hello = await _receive_json(ws)
    await ws.close()

    assert hello == {
        "type": "hello",
        "version": 1,
        "events": list(SUPPORTED_WEBSOCKET_EVENTS),
        "selected_events": list(SUPPORTED_WEBSOCKET_EVENTS),
    }


async def test_ws_event_query_filters_to_selected_public_event_names(aiohttp_client):
    modem = _FakeModem()
    client = await aiohttp_client(create_app(modem, api_keys=["test-key"]))
    ws = await client.ws_connect(
        "/ws?events=sms.received,sms.delivery_report",
        headers={"Authorization": "Bearer test-key"},
    )
    hello = await _receive_json(ws)

    await modem.bus.emit(SignalQualityEvent(rssi=19, ber=3))
    await modem.bus.emit(SMSDeliveryReportEvent(reference=7, recipient="+15551230100", status="delivered"))
    await modem.bus.emit(IncomingSMSEvent(sender="+15551230100", body="one time code"))

    first_event = await _receive_json(ws)
    second_event = await _receive_json(ws)
    await ws.close()

    assert hello == {
        "type": "hello",
        "version": 1,
        "events": list(SUPPORTED_WEBSOCKET_EVENTS),
        "selected_events": ["sms.received", "sms.delivery_report"],
    }
    assert [first_event["type"], second_event["type"]] == [
        "sms.delivery_report",
        "sms.received",
    ]


async def test_ws_event_query_normalizes_duplicates_and_whitespace(aiohttp_client):
    client = await aiohttp_client(create_app(_FakeModem(), api_keys=["test-key"]))
    ws = await client.ws_connect(
        "/ws?events=%20sms.received%20,,sms.received,%20signal.quality%20",
        headers={"Authorization": "Bearer test-key"},
    )
    hello = await _receive_json(ws)
    await ws.close()

    assert hello["events"] == list(SUPPORTED_WEBSOCKET_EVENTS)
    assert hello["selected_events"] == ["sms.received", "signal.quality"]


async def test_ws_modem_state_filter_subscribes_disconnect_and_reconnect_events(aiohttp_client):
    modem = _FakeModem()
    client = await aiohttp_client(create_app(modem))
    ws = await client.ws_connect("/ws?events=modem.state")
    hello = await _receive_json(ws)

    await modem.bus.emit(ModemDisconnectedEvent(reason="private serial detail"))
    await modem.bus.emit(ModemReconnectedEvent())

    disconnected = await _receive_json(ws)
    reconnected = await _receive_json(ws)
    await ws.close()

    assert hello["selected_events"] == ["modem.state"]
    assert disconnected["type"] == "modem.state"
    assert disconnected["data"] == {"connected": False}
    assert reconnected["type"] == "modem.state"
    assert reconnected["data"] == {"connected": True}


async def test_ws_rejects_unknown_event_names_before_subscribing(aiohttp_client):
    modem = _FakeModem()
    client = await aiohttp_client(create_app(modem, api_keys=["test-key"]))
    subscriber_counts = {
        event_type: len(handlers)
        for event_type, handlers in modem.bus._subscribers.items()
    }

    response = await client.get(
        "/ws?events=sms.received,raw.at",
        headers={"Authorization": "Bearer test-key"},
    )

    assert response.status == 400
    body = await response.json()
    assert body == {
        "error": "unsupported WebSocket event filter",
        "supported_events": list(SUPPORTED_WEBSOCKET_EVENTS),
    }
    assert "raw.at" not in json.dumps(body)
    assert {
        event_type: len(handlers)
        for event_type, handlers in modem.bus._subscribers.items()
    } == subscriber_counts


async def test_ws_streams_serialized_sms_events_without_private_payloads(aiohttp_client):
    modem = _FakeModem()
    client = await aiohttp_client(create_app(modem, api_keys=["test-key"]))
    ws = await client.ws_connect("/ws", headers={"Authorization": "Bearer test-key"})
    await _receive_json(ws)  # hello

    raw_sender = "+15550100100"
    await modem.bus.emit(
        IncomingSMSEvent(
            sender=raw_sender,
            body="secret MFA code 123456",
            raw=f'+CMT: "{raw_sender}"\nsecret MFA code 123456',
        )
    )

    event = await _receive_json(ws)
    await ws.close()

    assert event["type"] == "sms.received"
    assert event["data"] == {
        "sender": "+***0100",
        "body": "[redacted]",
        "body_length": len("secret MFA code 123456"),
    }
    serialized = json.dumps(event)
    assert "secret MFA" not in serialized
    assert "123456" not in serialized
    assert raw_sender not in serialized
    assert "+CMT" not in serialized


async def test_ws_broadcasts_events_to_multiple_clients_without_cross_blocking(aiohttp_client):
    modem = _FakeModem()
    client = await aiohttp_client(create_app(modem))
    first = await client.ws_connect("/ws")
    second = await client.ws_connect("/ws")
    await _receive_json(first)  # hello
    await _receive_json(second)  # hello

    await modem.bus.emit(SignalQualityEvent(rssi=19, ber=3))

    first_event = await _receive_json(first)
    second_event = await _receive_json(second)
    await first.close()
    await second.close()

    assert first_event == {
        "type": "signal.quality",
        "timestamp": first_event["timestamp"],
        "data": {"rssi": 19, "ber": 3},
    }
    assert second_event == first_event


async def test_websocket_overflow_drops_oldest_and_queues_pii_safe_notice():
    queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1)
    queue.put_nowait({"type": "sms.received", "data": {"body": "secret MFA code 123456"}})

    _enqueue_websocket_envelope(queue, {"type": "signal.quality", "data": {"rssi": 19, "ber": 3}})

    assert queue.qsize() == 1
    overflow = queue.get_nowait()
    assert overflow == {"type": "overflow", "version": 1, "dropped": 1}
    assert "secret" not in json.dumps(overflow)
    assert "123456" not in json.dumps(overflow)


def test_resolve_websocket_queue_maxsize_returns_default_without_override():
    app = create_app(_FakeModem())

    assert _resolve_websocket_queue_maxsize(app) == 32


def test_resolve_websocket_queue_maxsize_returns_valid_positive_override():
    app = create_app(_FakeModem())
    app["callstack_ws_queue_size"] = 5

    assert _resolve_websocket_queue_maxsize(app) == 5


@pytest.mark.parametrize(
    "invalid_queue_size",
    [0, -1, -32, True, False, 2.5, "32", "not-a-number", None, float("nan"), float("inf")],
)
def test_resolve_websocket_queue_maxsize_rejects_unsafe_overrides(invalid_queue_size):
    app = create_app(_FakeModem())
    app["callstack_ws_queue_size"] = invalid_queue_size

    with pytest.raises(ValueError):
        _resolve_websocket_queue_maxsize(app)


@pytest.mark.parametrize(
    "invalid_queue_size",
    [0, -1, -32, True, False, 2.5, "32", "not-a-number", None, float("nan"), float("inf")],
)
async def test_ws_rejects_unsafe_queue_size_overrides_without_opening_subscription(
    aiohttp_client, invalid_queue_size
):
    modem = _FakeModem()
    app = create_app(modem, api_keys=["test-key"])
    app["callstack_ws_queue_size"] = invalid_queue_size
    client = await aiohttp_client(app)
    subscriber_counts_before = {
        event_type: len(handlers)
        for event_type, handlers in modem.bus._subscribers.items()
    }

    with pytest.raises(aiohttp.WSServerHandshakeError) as excinfo:
        await client.ws_connect("/ws", headers={"Authorization": "Bearer test-key"})

    assert excinfo.value.status == 500
    assert {
        event_type: len(handlers)
        for event_type, handlers in modem.bus._subscribers.items()
    } == subscriber_counts_before


async def test_ws_accepts_valid_queue_size_override_and_still_streams_events(aiohttp_client):
    modem = _FakeModem()
    app = create_app(modem, api_keys=["test-key"])
    app["callstack_ws_queue_size"] = 2
    client = await aiohttp_client(app)

    ws = await client.ws_connect("/ws", headers={"Authorization": "Bearer test-key"})
    hello = await _receive_json(ws)

    await modem.bus.emit(SignalQualityEvent(rssi=19, ber=3))
    event = await _receive_json(ws)
    await ws.close()

    assert hello["selected_events"] == list(SUPPORTED_WEBSOCKET_EVENTS)
    assert event["type"] == "signal.quality"


def _record_queue_construction(monkeypatch, captured_maxsizes):
    real_queue_cls = asyncio.Queue

    class _RecordingQueue(real_queue_cls):
        def __init__(self, *args, maxsize=0, **kwargs):
            captured_maxsizes.append(maxsize)
            super().__init__(*args, maxsize=maxsize, **kwargs)

    monkeypatch.setattr(server.asyncio, "Queue", _RecordingQueue)


async def test_ws_endpoint_constructs_queue_with_default_maxsize_of_32(aiohttp_client, monkeypatch):
    captured_maxsizes = []
    _record_queue_construction(monkeypatch, captured_maxsizes)
    client = await aiohttp_client(create_app(_FakeModem(), api_keys=["test-key"]))

    ws = await client.ws_connect("/ws", headers={"Authorization": "Bearer test-key"})
    await _receive_json(ws)  # hello
    await ws.close()

    assert captured_maxsizes == [32]


async def test_ws_endpoint_constructs_queue_with_configured_override_maxsize(aiohttp_client, monkeypatch):
    captured_maxsizes = []
    _record_queue_construction(monkeypatch, captured_maxsizes)
    app = create_app(_FakeModem(), api_keys=["test-key"])
    app["callstack_ws_queue_size"] = 7
    client = await aiohttp_client(app)

    ws = await client.ws_connect("/ws", headers={"Authorization": "Bearer test-key"})
    await _receive_json(ws)  # hello
    await ws.close()

    assert captured_maxsizes == [7]
