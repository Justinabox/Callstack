"""Authenticated PII-safe WebSocket realtime endpoint tests."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

import server as server_module
from callstack.events.bus import EventBus
from callstack.events.serialize import serialize_event
from callstack.events.types import IncomingSMSEvent, SignalQualityEvent, USSDResponseEvent
from server import SUPPORTED_WEBSOCKET_EVENTS, _enqueue_websocket_envelope, create_app


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
        "cursor": 0,
        "replay_window": 128,
    }


async def test_ws_streams_serialized_sms_events_without_private_payloads(aiohttp_client):
    modem = _FakeModem()
    client = await aiohttp_client(create_app(modem, api_keys=["test-key"]))
    ws = await client.ws_connect("/ws", headers={"Authorization": "Bearer test-key"})
    await _receive_json(ws)  # hello

    raw_sender = "+15555550100"
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
        "id": 1,
        "type": "signal.quality",
        "timestamp": first_event["timestamp"],
        "data": {"rssi": 19, "ber": 3},
    }
    assert second_event == first_event


async def test_ws_replays_buffered_events_newer_than_since_cursor(aiohttp_client):
    modem = _FakeModem()
    client = await aiohttp_client(create_app(modem))
    first = await client.ws_connect("/ws")
    hello = await _receive_json(first)
    await first.close()

    raw_sender = "+15555550100"
    await modem.bus.emit(
        IncomingSMSEvent(
            sender=raw_sender,
            body="secret reconnect code 123456",
            raw=f'+CMT: "{raw_sender}"\nsecret reconnect code 123456',
        )
    )

    reconnect = await client.ws_connect(f"/ws?since={hello['cursor']}")
    reconnect_hello = await _receive_json(reconnect)
    replayed = await _receive_json(reconnect)
    await reconnect.close()

    assert reconnect_hello["cursor"] == 1
    assert replayed["id"] == 1
    assert replayed["type"] == "sms.received"
    assert replayed["data"] == {
        "sender": "+***0100",
        "body": "[redacted]",
        "body_length": len("secret reconnect code 123456"),
    }
    serialized = json.dumps(replayed)
    assert "secret reconnect" not in serialized
    assert "123456" not in serialized
    assert raw_sender not in serialized
    assert "+CMT" not in serialized


async def test_ws_replay_redacts_ussd_text_and_private_markers(aiohttp_client):
    modem = _FakeModem()
    client = await aiohttp_client(create_app(modem))

    await modem.bus.emit(
        USSDResponseEvent(
            status=0,
            message="Balance for SIM ICCID 89014103211118510720 is secret 123456",
            encoding=15,
        )
    )

    ws = await client.ws_connect("/ws?since=0")
    hello = await _receive_json(ws)
    replayed = await _receive_json(ws)
    await ws.close()

    assert hello["cursor"] == 1
    assert replayed["id"] == 1
    assert replayed["type"] == "ussd.response"
    serialized = json.dumps([hello, replayed])
    assert "Balance" not in serialized
    assert "89014103211118510720" not in serialized
    assert "123456" not in serialized
    assert "+CUSD" not in serialized
    assert "api-key" not in serialized.lower()


async def test_ws_rejects_invalid_since_before_upgrade(aiohttp_client):
    client = await aiohttp_client(create_app(_FakeModem()))

    with pytest.raises(aiohttp.WSServerHandshakeError) as excinfo:
        await client.ws_connect("/ws?since=not-a-cursor")

    assert excinfo.value.status == 400


async def test_ws_hello_cursor_matches_replay_snapshot_when_live_event_arrives_before_hello(
    aiohttp_client,
    monkeypatch,
):
    modem = _FakeModem()
    original_snapshot = server_module._websocket_replay_snapshot

    def snapshot_then_record_live_event(app, since):
        snapshot = original_snapshot(app, since)
        envelope = serialize_event(SignalQualityEvent(rssi=19, ber=3))
        replay_envelope = server_module._record_websocket_envelope(app, envelope)
        for queue in list(app["callstack_ws_state"]["queues"]):
            server_module._enqueue_websocket_envelope(queue, replay_envelope)
        return snapshot

    monkeypatch.setattr(
        server_module,
        "_websocket_replay_snapshot",
        snapshot_then_record_live_event,
    )
    client = await aiohttp_client(create_app(modem))

    ws = await client.ws_connect("/ws?since=0")
    hello = await _receive_json(ws)
    live_event = await _receive_json(ws)
    await ws.close()

    assert hello["cursor"] == 0
    assert live_event["id"] == 1
    assert live_event["type"] == "signal.quality"


async def test_ws_replay_recorder_unsubscribes_on_app_cleanup():
    modem = _FakeModem()
    app = create_app(modem)
    signal_subscriber_count = len(modem.bus._subscribers[SignalQualityEvent])

    app.freeze()
    await app.startup()
    await app.cleanup()

    assert len(modem.bus._subscribers[SignalQualityEvent]) == signal_subscriber_count - 1


async def test_ws_sends_pii_safe_replay_gap_for_too_old_cursor(aiohttp_client):
    modem = _FakeModem()
    app = create_app(modem)
    app["callstack_ws_replay_size"] = 1
    client = await aiohttp_client(app)

    raw_sender = "+15555550100"
    await modem.bus.emit(
        IncomingSMSEvent(
            sender=raw_sender,
            body="first secret code 111111",
            raw=f'+CMT: "{raw_sender}"\nfirst secret code 111111',
        )
    )
    await modem.bus.emit(SignalQualityEvent(rssi=19, ber=3))

    ws = await client.ws_connect("/ws?since=0")
    hello = await _receive_json(ws)
    gap = await _receive_json(ws)
    replayed = await _receive_json(ws)
    await ws.close()

    assert hello["cursor"] == 2
    assert gap == {"type": "replay_gap", "version": 1, "oldest": 2, "requested": 0}
    assert replayed["id"] == 2
    assert replayed["type"] == "signal.quality"
    serialized_gap = json.dumps(gap)
    assert "first secret" not in serialized_gap
    assert "111111" not in serialized_gap
    assert raw_sender not in serialized_gap


async def test_websocket_overflow_drops_oldest_and_queues_pii_safe_notice():
    queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1)
    queue.put_nowait({"type": "sms.received", "data": {"body": "secret MFA code 123456"}})

    _enqueue_websocket_envelope(queue, {"type": "signal.quality", "data": {"rssi": 19, "ber": 3}})

    assert queue.qsize() == 1
    overflow = queue.get_nowait()
    assert overflow == {"type": "overflow", "version": 1, "dropped": 1}
    assert "secret" not in json.dumps(overflow)
    assert "123456" not in json.dumps(overflow)
