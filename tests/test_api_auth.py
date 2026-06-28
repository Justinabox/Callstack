"""Tests for API key authentication middleware."""

import logging
import time
from types import SimpleNamespace
from typing import cast

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer

# Import directly to avoid needing full modem setup
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from callstack.events.bus import EventBus
from callstack.protocol.executor import ATCommandExecutor
from callstack.ussd import USSDService
import server
from server import APIKeyAuth, create_app


@pytest.fixture
def auth():
    return APIKeyAuth(api_keys=["test-key-123", "another-key"])


@pytest.fixture
def no_auth():
    return APIKeyAuth()


def _make_app(auth_instance: APIKeyAuth) -> web.Application:
    app = web.Application(middlewares=[auth_instance.middleware])

    async def hello(request):
        return web.json_response({"status": "ok"})

    app.router.add_get("/test", hello)
    return app


class TestAPIKeyAuthDisabled:
    async def test_no_keys_passes_through(self, aiohttp_client, no_auth):
        client = await aiohttp_client(_make_app(no_auth))
        resp = await client.get("/test")
        assert resp.status == 200

    async def test_disabled_by_default(self, no_auth):
        assert no_auth.enabled is False


class TestAPIKeyAuthEnabled:
    async def test_missing_header_returns_401(self, aiohttp_client, auth):
        client = await aiohttp_client(_make_app(auth))
        resp = await client.get("/test")
        assert resp.status == 401

    async def test_invalid_key_returns_403(self, aiohttp_client, auth):
        client = await aiohttp_client(_make_app(auth))
        resp = await client.get("/test", headers={"Authorization": "Bearer wrong-key"})
        assert resp.status == 403

    async def test_valid_key_passes(self, aiohttp_client, auth):
        client = await aiohttp_client(_make_app(auth))
        resp = await client.get("/test", headers={"Authorization": "Bearer test-key-123"})
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"

    async def test_second_valid_key(self, aiohttp_client, auth):
        client = await aiohttp_client(_make_app(auth))
        resp = await client.get("/test", headers={"Authorization": "Bearer another-key"})
        assert resp.status == 200

    async def test_malformed_header_returns_401(self, aiohttp_client, auth):
        client = await aiohttp_client(_make_app(auth))
        resp = await client.get("/test", headers={"Authorization": "Basic abc123"})
        assert resp.status == 401

    def test_blank_configured_key_is_rejected(self):
        with pytest.raises(ValueError, match="API key must not be blank"):
            APIKeyAuth(api_keys=[""])

    def test_whitespace_configured_key_is_rejected(self):
        with pytest.raises(ValueError, match="API key must not be blank"):
            APIKeyAuth(api_keys=["   \t"])


class TestAPIKeyConstantTimeComparison:
    def test_helper_compares_candidate_against_each_stored_key_without_self_compare(self, monkeypatch):
        auth = APIKeyAuth(api_keys=["test-key-123", "another-key"])
        calls = []

        def fake_compare_digest(left, right):
            calls.append((left, right))
            return left == right

        monkeypatch.setattr(server.secrets, "compare_digest", fake_compare_digest)

        assert auth._is_valid_key("wrong-key") is False

        assert len(calls) == 2
        assert set(calls) == {
            ("wrong-key", "test-key-123"),
            ("wrong-key", "another-key"),
        }
        assert ("wrong-key", "wrong-key") not in calls

    def test_helper_does_not_short_circuit_after_valid_key_match(self, monkeypatch):
        auth = APIKeyAuth(api_keys=["matching-key", "other-key"])
        auth._keys = cast(set[str], ("matching-key", "other-key"))
        calls = []

        def fake_compare_digest(left, right):
            calls.append((left, right))
            return left == right

        monkeypatch.setattr(server.secrets, "compare_digest", fake_compare_digest)

        assert auth._is_valid_key("matching-key") is True

        assert calls == [
            ("matching-key", "matching-key"),
            ("matching-key", "other-key"),
        ]


class TestAPIKeyManagement:
    def test_add_key(self):
        auth = APIKeyAuth()
        assert auth.enabled is False
        auth.add_key("new-key")
        assert auth.enabled is True
        assert "new-key" in auth._keys

    def test_add_key_rejects_blank_key(self):
        auth = APIKeyAuth()
        with pytest.raises(ValueError, match="API key must not be blank"):
            auth.add_key("")
        assert auth.enabled is False

    def test_add_key_rejects_whitespace_key(self):
        auth = APIKeyAuth()
        with pytest.raises(ValueError, match="API key must not be blank"):
            auth.add_key("  \n")
        assert auth.enabled is False

    async def test_revoke_key_invalidates_key_while_preserving_remaining_keys(self, aiohttp_client):
        auth = APIKeyAuth(api_keys=["revoked-key", "remaining-key"])
        client = await aiohttp_client(_make_app(auth))

        auth.revoke_key("revoked-key")

        revoked = await client.get("/test", headers={"Authorization": "Bearer revoked-key"})
        assert revoked.status == 403
        remaining = await client.get("/test", headers={"Authorization": "Bearer remaining-key"})
        assert remaining.status == 200
        assert auth.enabled is True

    async def test_revoke_last_key_keeps_middleware_fail_closed(self, aiohttp_client):
        auth = APIKeyAuth(api_keys=["only-key"])
        client = await aiohttp_client(_make_app(auth))

        auth.revoke_key("only-key")

        missing = await client.get("/test")
        assert missing.status == 401
        invalid = await client.get("/test", headers={"Authorization": "Bearer wrong-key"})
        assert invalid.status == 403
        assert auth.enabled is True

    async def test_add_replacement_key_after_last_revoke_restores_access(self, aiohttp_client):
        auth = APIKeyAuth(api_keys=["old-key"])
        client = await aiohttp_client(_make_app(auth))
        auth.revoke_key("old-key")

        auth.add_key("replacement-key")

        old_key = await client.get("/test", headers={"Authorization": "Bearer old-key"})
        assert old_key.status == 403
        replacement = await client.get("/test", headers={"Authorization": "Bearer replacement-key"})
        assert replacement.status == 200
        assert auth.enabled is True

    def test_revoke_nonexistent_key(self):
        auth = APIKeyAuth(api_keys=["key1"])
        auth.revoke_key("nonexistent")
        assert auth.enabled is True


class TestRateLimiting:
    async def test_rate_limit_exceeded(self, aiohttp_client):
        auth = APIKeyAuth(api_keys=["key"], rate_limit=3, rate_window=60)
        client = await aiohttp_client(_make_app(auth))
        headers = {"Authorization": "Bearer key"}

        for _ in range(3):
            resp = await client.get("/test", headers=headers)
            assert resp.status == 200

        resp = await client.get("/test", headers=headers)
        assert resp.status == 429
        data = await resp.json()
        assert "Rate limit" in data["error"]


class TestServerPrivacyLogging:
    async def test_webhook_failure_log_redacts_url_and_exception_details(self, monkeypatch, caplog):
        raw_url = "https://hooks.example.test/tenant/secret-token?api_key=super-secret&phone=15551234567"
        webhook_urls_before = list(server.webhook_urls)
        server.webhook_urls[:] = [raw_url]

        class FakeClientSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def post(self, url, **_kwargs):
                assert url == raw_url
                raise RuntimeError("delivery failed for api_key=super-secret phone=15551234567")

        monkeypatch.setattr(server.aiohttp, "ClientSession", FakeClientSession)

        try:
            with caplog.at_level(logging.WARNING, logger="server"):
                await server.notify_webhooks("+15551234567", "private sms body secret")
        finally:
            server.webhook_urls[:] = webhook_urls_before

        assert "Webhook POST" in caplog.text
        assert raw_url not in caplog.text
        assert "super-secret" not in caplog.text
        assert "15551234567" not in caplog.text
        assert "private sms body secret" not in caplog.text
        assert "RuntimeError" in caplog.text

    async def test_webhook_failure_log_handles_malformed_port_without_leaking(self, monkeypatch, caplog):
        raw_url = "https://hooks.example.test:notaport/tenant?api_key=super-secret"
        webhook_urls_before = list(server.webhook_urls)
        server.webhook_urls[:] = [raw_url]

        class FakeClientSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def post(self, url, **_kwargs):
                assert url == raw_url
                raise RuntimeError("delivery failed for api_key=super-secret")

        monkeypatch.setattr(server.aiohttp, "ClientSession", FakeClientSession)

        try:
            with caplog.at_level(logging.WARNING, logger="server"):
                await server.notify_webhooks("+15551234567", "private sms body secret")
        finally:
            server.webhook_urls[:] = webhook_urls_before

        assert "Webhook POST" in caplog.text
        assert raw_url not in caplog.text
        assert "notaport" not in caplog.text
        assert "super-secret" not in caplog.text
        assert "private sms body secret" not in caplog.text


class TestUSSDEndpointValidation:
    async def test_ussd_validation_error_returns_400_json_without_modem_write(self, aiohttp_client):
        class RecordingExecutor:
            def __init__(self):
                self.commands = []

            async def execute(self, command, **_kwargs):
                self.commands.append(command)
                raise AssertionError("USSD validation should run before modem writes")

        executor = RecordingExecutor()
        modem = SimpleNamespace(ussd=USSDService(cast(ATCommandExecutor, executor), EventBus()))
        client = await aiohttp_client(create_app(modem))

        resp = await client.post("/ussd/send", json={"code": "*100#\rAT+CMGD=1,4"})

        assert resp.status == 400
        data = await resp.json()
        assert data == {"error": "Invalid USSD code"}
        assert executor.commands == []
