"""Tests for API key authentication middleware."""

import logging
from datetime import datetime, timezone
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
from callstack.events.types import _RawSMSNotification
from callstack.protocol.executor import ATCommandExecutor
from callstack.sms.service import SMSService
from callstack.sms.store import SMSStore
from callstack.sms.types import DeliveryReport, SMS
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


class TestServerSMSRecording:
    @pytest.mark.parametrize("has_durable_history", [True, False])
    async def test_run_server_records_messages_globally_only_for_legacy_sms_services(
        self, monkeypatch, has_durable_history
    ):
        previous_messages = list(server.received_messages)
        server.received_messages.clear()
        webhook_calls = []

        async def record_webhook(sender, body):
            webhook_calls.append((sender, body))

        class FakeSMS:
            def on_message(self, callback):
                self.callback = callback

        fake_sms = FakeSMS()
        if has_durable_history:
            async def list_persisted_messages(limit=100):
                return []

            setattr(fake_sms, "list_persisted_messages", list_persisted_messages)

        class FakeModem:
            instance = None

            def __init__(self, _config):
                self.sms = fake_sms
                self.bus = EventBus()
                FakeModem.instance = self

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            def on_call(self, callback):
                return callback

            async def run_forever(self):
                await self.sms.callback(
                    server.IncomingSMSEvent(
                        sender="5551234",
                        body="inbound message",
                        timestamp=datetime(2026, 7, 27, tzinfo=timezone.utc),
                    )
                )

        class FakeRunner:
            def __init__(self, _app):
                pass

            async def setup(self):
                pass

            async def cleanup(self):
                pass

        class FakeSite:
            def __init__(self, _runner, _host, _port):
                pass

            async def start(self):
                pass

        monkeypatch.setattr(server, "Modem", FakeModem)
        monkeypatch.setattr(server, "notify_webhooks", record_webhook)
        monkeypatch.setattr(server, "create_app", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(server.web, "AppRunner", FakeRunner)
        monkeypatch.setattr(server.web, "TCPSite", FakeSite)
        try:
            await server.run_server(cast(server.ModemConfig, SimpleNamespace()))
        finally:
            recorded_messages = list(server.received_messages)
            server.received_messages[:] = previous_messages

        assert webhook_calls == [("5551234", "inbound message")]
        if has_durable_history:
            assert recorded_messages == []
        else:
            assert recorded_messages == [
                {
                    "sender": "5551234",
                    "body": "inbound message",
                    "received_at": "2026-07-27T00:00:00+00:00",
                }
            ]

    async def test_run_server_records_unpersisted_durable_sms_events_for_http_fallback(
        self, monkeypatch
    ):
        previous_messages = list(server.received_messages)
        server.received_messages.clear()
        webhook_calls = []

        async def record_webhook(sender, body):
            webhook_calls.append((sender, body))

        class FakeSMS:
            async def list_persisted_messages(self, limit=100):
                return []

            def on_message(self, callback):
                self.callback = callback

        fake_sms = FakeSMS()

        class FakeModem:
            def __init__(self, _config):
                self.sms = fake_sms
                self.bus = EventBus()

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            def on_call(self, callback):
                return callback

            async def run_forever(self):
                await self.sms.callback(
                    server.IncomingSMSEvent(
                        sender="5551234",
                        body="unpersisted direct message",
                        timestamp=datetime(2026, 7, 27, tzinfo=timezone.utc),
                        persisted=False,
                    )
                )

        class FakeRunner:
            def __init__(self, _app):
                pass

            async def setup(self):
                pass

            async def cleanup(self):
                pass

        class FakeSite:
            def __init__(self, _runner, _host, _port):
                pass

            async def start(self):
                pass

        monkeypatch.setattr(server, "Modem", FakeModem)
        monkeypatch.setattr(server, "notify_webhooks", record_webhook)
        monkeypatch.setattr(server, "create_app", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(server.web, "AppRunner", FakeRunner)
        monkeypatch.setattr(server.web, "TCPSite", FakeSite)
        try:
            await server.run_server(cast(server.ModemConfig, SimpleNamespace()))
        finally:
            recorded_messages = list(server.received_messages)
            server.received_messages[:] = previous_messages

        assert webhook_calls == [("5551234", "unpersisted direct message")]
        assert recorded_messages == [
            {
                "sender": "5551234",
                "body": "unpersisted direct message",
                "received_at": "2026-07-27T00:00:00+00:00",
            }
        ]


class TestSMSMessagesEndpoint:
    async def test_messages_endpoint_reads_only_inbound_persisted_history_in_legacy_shape(self, aiohttp_client):
        class FakeSMS:
            def __init__(self):
                self.limits = []

            async def list_persisted_messages(self, limit=100):
                self.limits.append(limit)
                return [
                    SMS(
                        id=7,
                        sender="5551234",
                        recipient="",
                        body="durable message",
                        timestamp=datetime(2026, 7, 27, tzinfo=timezone.utc),
                        status="unread",
                        reference=0,
                        storage_index=4,
                    ),
                    SMS(
                        id=8,
                        recipient="5556789",
                        body="outbound message",
                        timestamp=datetime(2026, 7, 27, tzinfo=timezone.utc),
                        status="sent",
                        reference=8,
                    ),
                ]

        fake_sms = FakeSMS()
        modem = SimpleNamespace(sms=fake_sms, ussd=SimpleNamespace(), bus=EventBus(), connected=True)
        client = await aiohttp_client(create_app(modem))

        resp = await client.get("/sms/messages?limit=2")

        assert resp.status == 200
        assert fake_sms.limits == [2]
        assert await resp.json() == [
            {
                "sender": "5551234",
                "body": "durable message",
                "received_at": "2026-07-27T00:00:00+00:00",
            }
        ]

    async def test_messages_endpoint_merges_interleaved_durable_and_fallback_history_chronologically(self, aiohttp_client):
        previous_messages = list(server.received_messages)
        server.received_messages[:] = [
            {"sender": "111", "body": "first fallback", "received_at": "2026-07-27T00:00:00+00:00"},
            {"sender": "222", "body": "middle fallback", "received_at": "2026-07-27T00:02:00+00:00"},
        ]

        class FakeSMS:
            async def list_persisted_messages(self, limit=100):
                return [
                    SMS(
                        sender="333",
                        body="older durable inbound",
                        timestamp=datetime(2026, 7, 27, 0, 1, tzinfo=timezone.utc),
                        status="unread",
                    ),
                    SMS(
                        sender="444",
                        body="newest durable inbound",
                        timestamp=datetime(2026, 7, 27, 0, 3, tzinfo=timezone.utc),
                        status="unread",
                    )
                ]

        modem = SimpleNamespace(sms=FakeSMS(), ussd=SimpleNamespace(), bus=EventBus(), connected=True)
        client = await aiohttp_client(create_app(modem))
        try:
            resp = await client.get("/sms/messages?limit=1")
            payload = await resp.json()
        finally:
            server.received_messages[:] = previous_messages

        assert resp.status == 200
        assert payload == [
            {"sender": "444", "body": "newest durable inbound", "received_at": "2026-07-27T00:03:00+00:00"}
        ]

    async def test_messages_endpoint_treats_utc_overflow_legacy_fallback_timestamp_as_oldest(
        self, aiohttp_client
    ):
        previous_messages = list(server.received_messages)
        server.received_messages[:] = [
            {
                "sender": "111",
                "body": "UTC overflow fallback",
                "received_at": "0001-01-01T00:00:00+23:59",
            }
        ]

        class FakeSMS:
            async def list_persisted_messages(self, limit=100):
                return [
                    SMS(
                        sender="222",
                        body="newer durable inbound",
                        timestamp=datetime(2026, 7, 27, tzinfo=timezone.utc),
                        status="unread",
                    )
                ]

        modem = SimpleNamespace(sms=FakeSMS(), ussd=SimpleNamespace(), bus=EventBus(), connected=True)
        client = await aiohttp_client(create_app(modem))
        try:
            resp = await client.get("/sms/messages?limit=1")
            payload = await resp.json()
        finally:
            server.received_messages[:] = previous_messages

        assert resp.status == 200
        assert payload == [
            {
                "sender": "222",
                "body": "newer durable inbound",
                "received_at": "2026-07-27T00:00:00+00:00",
            }
        ]

    async def test_messages_endpoint_serializes_direct_delivery_timestamp_as_aware_utc(
        self, aiohttp_client
    ):
        previous_messages = list(server.received_messages)
        server.received_messages.clear()
        try:
            store = SMSStore()
            bus = EventBus()
            sms = SMSService(cast(ATCommandExecutor, SimpleNamespace()), bus, store)
            await sms._on_incoming(
                _RawSMSNotification(
                    sender="5551234",
                    body="direct message",
                    raw='+CMT: "5551234","","26/07/27,00:00:00+00"',
                )
            )
            modem = SimpleNamespace(sms=sms, ussd=SimpleNamespace(), bus=bus, connected=True)
            client = await aiohttp_client(create_app(modem))

            resp = await client.get("/sms/messages?limit=1")
            payload = await resp.json()
        finally:
            server.received_messages[:] = previous_messages

        assert resp.status == 200
        assert payload[0]["sender"] == "5551234"
        assert payload[0]["body"] == "direct message"
        assert set(payload[0]) == {"sender", "body", "received_at"}
        assert datetime.fromisoformat(payload[0]["received_at"]).tzinfo == timezone.utc

    async def test_messages_endpoint_legacy_fallback_is_bounded_and_preserves_legacy_shape(self, aiohttp_client):
        previous_messages = list(server.received_messages)
        server.received_messages[:] = [
            {"sender": "111", "body": "first", "received_at": "2026-07-27T00:00:00+00:00"},
            {"sender": "222", "body": "second", "received_at": "2026-07-27T00:01:00+00:00"},
            {"sender": "333", "body": "third", "received_at": "2026-07-27T00:02:00+00:00"},
        ]
        modem = SimpleNamespace(sms=SimpleNamespace(), ussd=SimpleNamespace(), bus=EventBus(), connected=True)
        client = await aiohttp_client(create_app(modem))
        try:
            resp = await client.get("/sms/messages?limit=2")
            payload = await resp.json()
        finally:
            server.received_messages[:] = previous_messages

        assert resp.status == 200
        assert payload == [
            {"sender": "222", "body": "second", "received_at": "2026-07-27T00:01:00+00:00"},
            {"sender": "333", "body": "third", "received_at": "2026-07-27T00:02:00+00:00"},
        ]

    async def test_messages_endpoint_uses_safe_default_limit_for_persisted_history(self, aiohttp_client):
        class FakeSMS:
            def __init__(self):
                self.limits = []

            async def list_persisted_messages(self, limit=100):
                self.limits.append(limit)
                return []

        fake_sms = FakeSMS()
        modem = SimpleNamespace(sms=fake_sms, ussd=SimpleNamespace(), bus=EventBus(), connected=True)
        client = await aiohttp_client(create_app(modem))

        resp = await client.get("/sms/messages")

        assert resp.status == 200
        assert fake_sms.limits == [50]
        assert await resp.json() == []

    async def test_messages_endpoint_rejects_invalid_limit_before_reading_persisted_history(self, aiohttp_client):
        class FakeSMS:
            def __init__(self):
                self.limits = []

            async def list_persisted_messages(self, limit=100):
                self.limits.append(limit)
                raise AssertionError("invalid limits must not reach persisted history")

        fake_sms = FakeSMS()
        modem = SimpleNamespace(sms=fake_sms, ussd=SimpleNamespace(), bus=EventBus(), connected=True)
        client = await aiohttp_client(create_app(modem))

        resp = await client.get("/sms/messages?limit=0")

        assert resp.status == 400
        assert await resp.json() == {"error": "invalid 'limit'"}
        assert fake_sms.limits == []

    async def test_messages_endpoint_preserves_sqlite_history_across_app_recreation(self, aiohttp_client, tmp_path):
        db_path = str(tmp_path / "sms.db")
        expected_history = [
            {
                "sender": "5551234",
                "body": "saved before restart",
                "received_at": "2026-07-27T00:00:00+00:00",
            }
        ]

        store_before_restart = SMSStore(db_path=db_path)
        try:
            await store_before_restart.initialize()
            await store_before_restart.save(
                SMS(
                    sender="5551234",
                    body="saved before restart",
                    timestamp=datetime(2026, 7, 27, tzinfo=timezone.utc),
                    status="unread",
                )
            )
            await store_before_restart.save(
                SMS(recipient="5556789", body="outbound before restart", status="sent")
            )
            sms_before_restart = SMSService(
                cast(ATCommandExecutor, SimpleNamespace()), EventBus(), store_before_restart
            )
            modem_before_restart = SimpleNamespace(
                sms=sms_before_restart, ussd=SimpleNamespace(), bus=EventBus(), connected=True
            )
            client_before_restart = await aiohttp_client(create_app(modem_before_restart))
            response_before_restart = await client_before_restart.get("/sms/messages")

            assert response_before_restart.status == 200
            assert await response_before_restart.json() == expected_history
        finally:
            await store_before_restart.close()

        store_after_restart = SMSStore(db_path=db_path)
        try:
            await store_after_restart.initialize()
            sms_after_restart = SMSService(
                cast(ATCommandExecutor, SimpleNamespace()), EventBus(), store_after_restart
            )
            modem_after_restart = SimpleNamespace(
                sms=sms_after_restart, ussd=SimpleNamespace(), bus=EventBus(), connected=True
            )
            client_after_restart = await aiohttp_client(create_app(modem_after_restart))
            response_after_restart = await client_after_restart.get("/sms/messages")

            assert response_after_restart.status == 200
            assert await response_after_restart.json() == expected_history
        finally:
            await store_after_restart.close()


class TestDeliveryReportEndpoint:
    async def test_delivery_reports_endpoint_reads_sms_store_with_limit_and_redacts_recipient(self, aiohttp_client):
        class FakeSMS:
            def __init__(self):
                self.limits = []

            async def list_delivery_reports(self, limit=100):
                self.limits.append(limit)
                return [
                    DeliveryReport(
                        id=7,
                        reference=42,
                        recipient="+15551234567",
                        status="delivered",
                        timestamp=datetime(2026, 6, 28, tzinfo=timezone.utc),
                        message_id=3,
                    )
                ]

        fake_sms = FakeSMS()
        modem = SimpleNamespace(sms=fake_sms, ussd=SimpleNamespace(), bus=EventBus(), connected=True)
        client = await aiohttp_client(create_app(modem))

        resp = await client.get("/sms/delivery-reports?limit=1")

        assert resp.status == 200
        assert fake_sms.limits == [1]
        assert await resp.json() == [
            {
                "id": 7,
                "reference": 42,
                "recipient": "+***4567",
                "status": "delivered",
                "timestamp": "2026-06-28T00:00:00+00:00",
                "message_id": 3,
            }
        ]


    async def test_delivery_reports_endpoint_fallback_handles_legacy_dict_reports(self, aiohttp_client):
        previous_reports = list(server.delivery_reports)
        server.delivery_reports[:] = [
            {
                "id": 9,
                "reference": 43,
                "recipient": "+15551230000",
                "status": "failed",
                "timestamp": "2026-06-28T01:02:03+00:00",
                "message_id": 4,
            }
        ]
        modem = SimpleNamespace(sms=SimpleNamespace(), ussd=SimpleNamespace(), bus=EventBus(), connected=True)
        try:
            client = await aiohttp_client(create_app(modem))
            resp = await client.get("/sms/delivery-reports")
        finally:
            server.delivery_reports[:] = previous_reports

        assert resp.status == 200
        assert await resp.json() == [
            {
                "id": 9,
                "reference": 43,
                "recipient": "+***0000",
                "status": "failed",
                "timestamp": "2026-06-28T01:02:03+00:00",
                "message_id": 4,
            }
        ]


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
