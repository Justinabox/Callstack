"""Tests for API key authentication middleware."""

import time
import pytest
from unittest.mock import AsyncMock
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer

# Import directly to avoid needing full modem setup
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from server import APIKeyAuth


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


class TestAPIKeyManagement:
    def test_add_key(self):
        auth = APIKeyAuth()
        assert auth.enabled is False
        auth.add_key("new-key")
        assert auth.enabled is True
        assert "new-key" in auth._keys

    def test_revoke_key(self):
        auth = APIKeyAuth(api_keys=["only-key"])
        assert auth.enabled is True
        auth.revoke_key("only-key")
        assert auth.enabled is False

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
