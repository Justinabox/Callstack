"""PII-safe health and metrics endpoint tests."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from callstack.events.bus import EventBus
from callstack.events.types import (
    CallState,
    CallStateEvent,
    IncomingSMSEvent,
    ModemDisconnectedEvent,
    ModemReconnectedEvent,
    SMSDeliveryReportEvent,
    SMSSentEvent,
    SignalQualityEvent,
    USSDResponseEvent,
)
from server import create_app


class _FakeModem:
    def __init__(self, *, connected: bool = True):
        self.connected = connected
        self.bus = EventBus()
        self.sms = SimpleNamespace(send=AsyncMock(), store=object())
        self.ussd = SimpleNamespace(send=AsyncMock())


async def _drain_event_tasks() -> None:
    await asyncio.sleep(0)


async def test_healthz_reports_ready_when_modem_connected(aiohttp_client):
    client = await aiohttp_client(create_app(_FakeModem(connected=True)))

    response = await client.get("/healthz")

    assert response.status == 200
    payload = await response.json()
    assert payload["status"] == "ready"
    assert payload["modem_connected"] is True
    assert payload["sms_store_ready"] is True
    assert isinstance(payload["uptime_seconds"], float)
    assert payload["uptime_seconds"] >= 0


async def test_healthz_reports_degraded_when_modem_not_connected(aiohttp_client):
    client = await aiohttp_client(create_app(_FakeModem(connected=False)))

    response = await client.get("/healthz")

    assert response.status == 503
    payload = await response.json()
    assert payload["status"] == "degraded"
    assert payload["modem_connected"] is False


async def test_metrics_update_from_typed_events_without_pii(aiohttp_client):
    modem = _FakeModem(connected=True)
    client = await aiohttp_client(create_app(modem))

    await modem.bus.emit(IncomingSMSEvent(sender="+15551234567", body="secret MFA code 123456"))
    await modem.bus.emit(SMSSentEvent(recipient="+15557654321", reference=7))
    await modem.bus.emit(SMSDeliveryReportEvent(recipient="+15557654321", status="delivered"))
    await modem.bus.emit(SMSDeliveryReportEvent(recipient="+15557654321", status="failed +15550000000 secret"))
    await modem.bus.emit(CallStateEvent(state=CallState.ACTIVE))
    await modem.bus.emit(SignalQualityEvent(rssi=19, ber=3))
    await modem.bus.emit(ModemDisconnectedEvent(reason="lost modem IMEI 123456789012345"))
    await modem.bus.emit(ModemReconnectedEvent())
    await modem.bus.emit(USSDResponseEvent(status=0, message="balance for +15551234567 is private"))
    await _drain_event_tasks()

    response = await client.get("/metrics")

    assert response.status == 200
    assert response.content_type == "text/plain"
    body = await response.text()
    assert "# HELP callstack_uptime_seconds" in body
    assert "# TYPE callstack_sms_received_total counter" in body
    assert "callstack_sms_received_total 1" in body
    assert "callstack_sms_sent_total 1" in body
    assert 'callstack_sms_delivery_reports_total{status="delivered"} 1' in body
    assert 'callstack_sms_delivery_reports_total{status="unknown"} 1' in body
    assert "callstack_active_calls 1" in body
    assert "callstack_signal_rssi 19" in body
    assert "callstack_signal_ber 3" in body
    assert "callstack_modem_disconnects_total 1" in body
    assert "callstack_modem_reconnects_total 1" in body
    assert "callstack_ussd_responses_total 1" in body
    assert "+1555" not in body
    assert "secret" not in body
    assert "123456" not in body
    assert "IMEI" not in body


async def test_observability_routes_follow_api_key_auth_when_enabled(aiohttp_client):
    client = await aiohttp_client(create_app(_FakeModem(), api_keys=["test-key"]))

    unauthorized = await client.get("/metrics")
    authorized = await client.get("/metrics", headers={"Authorization": "Bearer test-key"})
    health_authorized = await client.get("/healthz", headers={"Authorization": "Bearer test-key"})

    assert unauthorized.status == 401
    assert authorized.status == 200
    assert health_authorized.status == 200
