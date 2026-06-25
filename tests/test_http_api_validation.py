"""HTTP API validation tests for client-side request errors."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server import create_app


class _FakeModem:
    def __init__(self):
        self.sms = SimpleNamespace(send=AsyncMock())
        self.ussd = SimpleNamespace(send=AsyncMock())


async def test_write_routes_return_json_400_for_malformed_json(aiohttp_client):
    modem = _FakeModem()
    client = await aiohttp_client(create_app(modem))

    for path in ("/sms/send", "/sms/subscribe", "/ussd/send"):
        response = await client.post(path, data="{", headers={"Content-Type": "application/json"})

        assert response.status == 400
        assert response.content_type == "application/json"
        payload = await response.json()
        assert payload == {"error": "invalid JSON body"}

    modem.sms.send.assert_not_awaited()
    modem.ussd.send.assert_not_awaited()


async def test_sms_send_rejects_invalid_phone_number_before_service_call(aiohttp_client):
    modem = _FakeModem()
    client = await aiohttp_client(create_app(modem))

    response = await client.post("/sms/send", json={"to": "bad\"number", "body": "hello"})

    assert response.status == 400
    assert response.content_type == "application/json"
    payload = await response.json()
    assert payload == {"error": "invalid 'to' phone number"}
    modem.sms.send.assert_not_awaited()


async def test_ussd_send_rejects_invalid_timeout_before_service_call(aiohttp_client):
    modem = _FakeModem()
    client = await aiohttp_client(create_app(modem))

    response = await client.post("/ussd/send", json={"code": "*100#", "timeout": "soon"})

    assert response.status == 400
    assert response.content_type == "application/json"
    payload = await response.json()
    assert payload == {"error": "invalid 'timeout'"}
    modem.ussd.send.assert_not_awaited()


@pytest.mark.parametrize("timeout", [float("nan"), float("inf")])
async def test_ussd_send_rejects_non_finite_timeout_before_service_call(aiohttp_client, timeout):
    modem = _FakeModem()
    client = await aiohttp_client(create_app(modem))

    response = await client.post("/ussd/send", json={"code": "*100#", "timeout": timeout})

    assert response.status == 400
    assert response.content_type == "application/json"
    payload = await response.json()
    assert payload == {"error": "invalid 'timeout'"}
    modem.ussd.send.assert_not_awaited()


@pytest.mark.parametrize(
    ("service_name", "path", "payload", "safe_error"),
    [
        ("sms", "/sms/send", {"to": "+15551234567", "body": "hello"}, "invalid SMS request"),
        ("ussd", "/ussd/send", {"code": "*100#"}, "invalid USSD request"),
    ],
)
async def test_backend_value_errors_return_safe_client_messages(
    aiohttp_client,
    service_name,
    path,
    payload,
    safe_error,
):
    modem = _FakeModem()
    getattr(modem, service_name).send.side_effect = ValueError("secret SIM +15551234567 backend detail")
    client = await aiohttp_client(create_app(modem))

    response = await client.post(path, json=payload)

    assert response.status == 400
    assert response.content_type == "application/json"
    body = await response.json()
    assert body == {"error": safe_error}
    assert "secret" not in str(body)
    assert "+15551234567" not in str(body)
