"""Webhook subscription admission-control tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server import create_app, webhook_urls


class _FakeModem:
    def __init__(self):
        self.sms = SimpleNamespace(send=AsyncMock())
        self.ussd = SimpleNamespace(send=AsyncMock())


@pytest.fixture(autouse=True)
def clear_webhook_urls():
    webhook_urls.clear()
    yield
    webhook_urls.clear()


@pytest.fixture
async def client(aiohttp_client):
    return await aiohttp_client(create_app(_FakeModem()))


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data?iam/security-credentials/role",
        "http://127.0.0.1/hook",
        "http://localhost/hook",
        "http://192.168.1.10/hook",
        "http://[::1]/hook",
        "http://[fe80::1]/hook",
        "https://user:pass@example.com/hook",
        "https://example.com/callstack-hook?token=secret",
        "https://example.com/callstack-hook#token=secret",
        "http://2130706433/hook",
        "http://0x7f000001/hook",
        "http://127.1/hook",
        "http://224.0.0.1/hook",
        "https://example.com:99999/hook",
        "https://example.com:0/hook",
        " https://example.com/hook",
        "https://example.com/hook\r\nHost: attacker.invalid",
        "http://localhost\u3002/hook",
        "http://127\u30020\u30020\u30021/hook",
        "http://\uff10x\uff17f\uff10\uff10\uff10\uff10\uff10\uff11/hook",
    ],
)
async def test_sms_subscribe_rejects_unsafe_webhook_urls_without_storing_or_echoing(client, url):
    response = await client.post("/sms/subscribe", json={"url": url})

    assert response.status == 400
    assert response.content_type == "application/json"
    payload = await response.json()
    assert payload == {"error": "invalid webhook URL"}
    assert url not in str(payload)
    assert webhook_urls == []


async def test_sms_subscribe_accepts_and_stores_public_https_url(client):
    safe_url = "https://example.com/callstack-hook"

    response = await client.post("/sms/subscribe", json={"url": safe_url})

    assert response.status == 200
    assert response.content_type == "application/json"
    payload = await response.json()
    assert payload == {"status": "subscribed", "url": safe_url}
    assert webhook_urls == [safe_url]
