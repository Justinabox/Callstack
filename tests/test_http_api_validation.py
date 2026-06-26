"""HTTP API validation tests for client-side request errors."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from callstack.errors import ATTimeoutError, SMSSendError, TransportError
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


@pytest.mark.parametrize("recipient", ["bad\"number", "++123", "12+34", "*123#", "+123\n", "+123\r"])
async def test_sms_send_rejects_invalid_phone_number_before_service_call(aiohttp_client, recipient):
    modem = _FakeModem()
    client = await aiohttp_client(create_app(modem))

    response = await client.post("/sms/send", json={"to": recipient, "body": "hello"})

    assert response.status == 400
    assert response.content_type == "application/json"
    payload = await response.json()
    assert payload == {"error": "invalid 'to' phone number"}
    modem.sms.send.assert_not_awaited()


@pytest.mark.parametrize("recipient", ["+15551234567", "5551234"])
async def test_sms_send_accepts_documented_phone_number_formats(aiohttp_client, recipient):
    modem = _FakeModem()
    modem.sms.send.return_value = SimpleNamespace(recipient=recipient, reference=42)
    client = await aiohttp_client(create_app(modem))

    response = await client.post("/sms/send", json={"to": recipient, "body": "hello"})

    assert response.status == 200
    payload = await response.json()
    assert payload == {"status": "sent", "to": recipient, "reference": 42}
    modem.sms.send.assert_awaited_once_with(recipient, "hello")


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
    getattr(modem, service_name).send.side_effect = ValueError("secret SIM +155****4567 backend detail")
    client = await aiohttp_client(create_app(modem))

    response = await client.post(path, json=payload)

    assert response.status == 400
    assert response.content_type == "application/json"
    body = await response.json()
    assert body == {"error": safe_error}
    assert "secret" not in str(body)
    assert "+155****4567" not in str(body)


@pytest.mark.parametrize(
    ("exception", "status", "safe_error"),
    [
        (SMSSendError("Failed to send SMS to private-recipient: ['ERROR']"), 502, "SMS send failed"),
        (TransportError("serial port for private-recipient disconnected"), 502, "SMS send failed"),
        (ATTimeoutError("AT+CMGS timed out for private-recipient"), 504, "SMS send timed out"),
        (TimeoutError("timeout while sending passcode 123456"), 504, "SMS send timed out"),
    ],
)
async def test_sms_backend_failures_return_redacted_json(aiohttp_client, exception, status, safe_error):
    modem = _FakeModem()
    modem.sms.send.side_effect = exception
    client = await aiohttp_client(create_app(modem))

    response = await client.post("/sms/send", json={"to": "5551234", "body": "hello"})

    assert response.status == status
    assert response.content_type == "application/json"
    body = await response.json()
    assert body == {"error": safe_error}
    response_text = str(body)
    assert "private-recipient" not in response_text
    assert "123456" not in response_text
    assert "AT+CMGS" not in response_text


async def test_sms_body_encoding_errors_return_client_error(aiohttp_client):
    modem = _FakeModem()
    modem.sms.send.side_effect = SMSSendError(
        "SMS body cannot be encoded with GSM 03.38 text mode; UCS2/PDU sending is not implemented yet"
    )
    client = await aiohttp_client(create_app(modem))

    response = await client.post("/sms/send", json={"to": "5551234", "body": "hello 🌍"})

    assert response.status == 400
    assert response.content_type == "application/json"
    body = await response.json()
    assert body == {"error": "invalid SMS request"}
    response_text = str(body)
    assert "GSM 03.38" not in response_text
    assert "UCS2" not in response_text


async def test_sms_unexpected_runtime_errors_are_not_masked_as_operational_failures(aiohttp_client):
    modem = _FakeModem()
    modem.sms.send.side_effect = RuntimeError("unexpected programming bug")
    client = await aiohttp_client(create_app(modem))

    response = await client.post("/sms/send", json={"to": "5551234", "body": "hello"})

    assert response.status == 500
    assert response.content_type != "application/json"


@pytest.mark.parametrize(
    ("exception", "status", "safe_error"),
    [
        (TimeoutError("USSD timeout for *123# on SIM +155****4567"), 504, "USSD request timed out"),
        (RuntimeError("modem backend detail includes AT+CUSD and passcode 123456"), 502, "USSD request failed"),
        (ATTimeoutError("AT+CUSD timed out for *123#"), 504, "USSD request timed out"),
        (TransportError("USSD serial port for +155****4567 disconnected"), 502, "USSD request failed"),
    ],
)
async def test_ussd_backend_failures_return_redacted_json(aiohttp_client, exception, status, safe_error):
    modem = _FakeModem()
    modem.ussd.send.side_effect = exception
    client = await aiohttp_client(create_app(modem))

    response = await client.post("/ussd/send", json={"code": "*123#"})

    assert response.status == status
    assert response.content_type == "application/json"
    body = await response.json()
    assert body == {"error": safe_error}
    response_text = str(body)
    assert "+155****4567" not in response_text
    assert "123456" not in response_text
    assert "AT+CUSD" not in response_text
    assert "*123#" not in response_text
