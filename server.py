"""Auto-answer calls + SMS HTTP server using Callstack."""

import asyncio
import json
import logging
import math
import secrets
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from callstack import Modem, ModemConfig, CallSession, IncomingSMSEvent
from callstack.errors import ATTimeoutError, SMSSendError, TransportError
from callstack.events.types import SMSDeliveryReportEvent
from callstack.metrics import CallstackMetrics
from callstack.privacy import redact_url_for_log
from callstack.protocol.commands import ATCommand

logger = logging.getLogger("server")

AUDIO_GREET = str(Path(__file__).parent / "audio" / "greet.wav")
HTTP_HOST = "0.0.0.0"
HTTP_PORT = 8080

# Webhook subscribers and received messages store
webhook_urls: list[str] = []
received_messages: list[dict] = []
delivery_reports: list[dict] = []


# -- API Key Authentication --

class APIKeyAuth:
    """API key authentication middleware with rate limiting.

    Keys are passed via the Authorization header as 'Bearer <key>'.

    Usage:
        auth = APIKeyAuth(api_keys=["my-secret-key"], rate_limit=60)
        app = web.Application(middlewares=[auth.middleware])
    """

    def __init__(
        self,
        api_keys: list[str] | None = None,
        rate_limit: int = 60,
        rate_window: int = 60,
    ):
        self._keys: set[str] = set()
        if api_keys:
            self._keys = {self._validated_key(key) for key in api_keys}
        self._rate_limit = rate_limit
        self._rate_window = rate_window
        self._request_log: dict[str, list[float]] = defaultdict(list)
        self.enabled = bool(self._keys)

    @web.middleware
    async def middleware(self, request: web.Request, handler) -> web.Response:
        if not self.enabled:
            return await handler(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return web.json_response(
                {"error": "Missing or invalid Authorization header. Use 'Bearer <api_key>'."},
                status=401,
            )

        key = auth_header[7:]
        if not self._is_valid_key(key):
            return web.json_response({"error": "Invalid API key."}, status=403)

        # Rate limiting
        now = time.monotonic()
        log = self._request_log[key]
        # Prune old entries
        cutoff = now - self._rate_window
        self._request_log[key] = [t for t in log if t > cutoff]
        log = self._request_log[key]

        if len(log) >= self._rate_limit:
            return web.json_response(
                {"error": "Rate limit exceeded.", "retry_after": self._rate_window},
                status=429,
            )
        log.append(now)

        return await handler(request)

    def _is_valid_key(self, candidate_key: str) -> bool:
        valid = False
        for stored_key in self._keys:
            valid |= secrets.compare_digest(candidate_key, stored_key)
        return valid

    @staticmethod
    def _validated_key(key: str) -> str:
        if not key.strip():
            raise ValueError("API key must not be blank")
        return key

    def add_key(self, key: str) -> None:
        self._keys.add(self._validated_key(key))
        self.enabled = True

    def revoke_key(self, key: str) -> None:
        self._keys.discard(key)
        self.enabled = bool(self._keys)


async def _json_body(request: web.Request) -> tuple[dict[str, Any] | None, web.Response | None]:
    try:
        data = await request.json()
    except (aiohttp.ContentTypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None, web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(data, dict):
        return None, web.json_response({"error": "JSON body must be an object"}, status=400)
    return data, None


def _required_string(data: dict[str, Any], field: str) -> tuple[str | None, web.Response | None]:
    value = data.get(field)
    if not isinstance(value, str) or not value:
        return None, web.json_response({"error": f"missing '{field}'"}, status=400)
    return value, None


def _optional_positive_timeout(
    data: dict[str, Any],
    field: str = "timeout",
    default: float = 15.0,
) -> tuple[float | None, web.Response | None]:
    value = data.get(field, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return None, web.json_response({"error": f"invalid '{field}'"}, status=400)
    return float(value), None


def _is_sms_body_encoding_error(exc: SMSSendError) -> bool:
    return "SMS body cannot be encoded" in exc.detail


def create_app(modem: Modem, api_keys: list[str] | None = None) -> web.Application:
    auth = APIKeyAuth(api_keys=api_keys)
    app = web.Application(middlewares=[auth.middleware])
    metrics = CallstackMetrics(modem)
    app["callstack_metrics"] = metrics

    async def healthz(request: web.Request) -> web.Response:
        return web.json_response(metrics.health_payload(), status=metrics.health_status())

    async def render_metrics(request: web.Request) -> web.Response:
        return web.Response(text=metrics.render_prometheus(), content_type="text/plain")

    async def send_sms(request: web.Request) -> web.Response:
        data, error = await _json_body(request)
        if error is not None:
            return error
        assert data is not None
        to, error = _required_string(data, "to")
        if error is not None:
            return error
        body, error = _required_string(data, "body")
        if error is not None:
            return error
        assert to is not None
        assert body is not None
        try:
            ATCommand.send_sms(to)
        except ValueError:
            return web.json_response({"error": "invalid 'to' phone number"}, status=400)
        try:
            sms = await modem.sms.send(to, body)
        except ValueError:
            return web.json_response({"error": "invalid SMS request"}, status=400)
        except (ATTimeoutError, TimeoutError):
            logger.warning("SMS send timed out; returning redacted HTTP 504")
            return web.json_response({"error": "SMS send timed out"}, status=504)
        except SMSSendError as exc:
            if _is_sms_body_encoding_error(exc):
                return web.json_response({"error": "invalid SMS request"}, status=400)
            logger.warning("SMS send failed; returning redacted HTTP 502")
            return web.json_response({"error": "SMS send failed"}, status=502)
        except TransportError:
            logger.warning("SMS send failed; returning redacted HTTP 502")
            return web.json_response({"error": "SMS send failed"}, status=502)
        return web.json_response({
            "status": "sent",
            "to": sms.recipient,
            "reference": sms.reference,
        })

    async def subscribe(request: web.Request) -> web.Response:
        data, error = await _json_body(request)
        if error is not None:
            return error
        assert data is not None
        url, error = _required_string(data, "url")
        if error is not None:
            return error
        assert url is not None
        webhook_urls.append(url)
        return web.json_response({"status": "subscribed", "url": url})

    async def list_messages(request: web.Request) -> web.Response:
        return web.json_response(received_messages)

    async def list_delivery_reports(request: web.Request) -> web.Response:
        return web.json_response(delivery_reports)

    async def ussd_send(request: web.Request) -> web.Response:
        data, error = await _json_body(request)
        if error is not None:
            return error
        assert data is not None
        code, error = _required_string(data, "code")
        if error is not None:
            return error
        assert code is not None
        timeout, error = _optional_positive_timeout(data)
        if error is not None:
            return error
        assert timeout is not None
        try:
            resp = await modem.ussd.send(code, timeout=timeout)
            return web.json_response({
                "status": resp.status,
                "message": resp.message,
            })
        except ValueError as exc:
            error_message = str(exc)
            if error_message not in {"Invalid USSD code", "Invalid USSD encoding"}:
                error_message = "invalid USSD request"
            return web.json_response({"error": error_message}, status=400)
        except (ATTimeoutError, TimeoutError):
            logger.warning("USSD request timed out; returning redacted HTTP 504")
            return web.json_response({"error": "USSD request timed out"}, status=504)
        except (TransportError, RuntimeError):
            logger.warning("USSD request failed; returning redacted HTTP 502")
            return web.json_response({"error": "USSD request failed"}, status=502)

    app.router.add_get("/healthz", healthz)
    app.router.add_get("/metrics", render_metrics)
    app.router.add_post("/sms/send", send_sms)
    app.router.add_post("/sms/subscribe", subscribe)
    app.router.add_get("/sms/messages", list_messages)
    app.router.add_get("/sms/delivery-reports", list_delivery_reports)
    app.router.add_post("/ussd/send", ussd_send)
    return app


async def notify_webhooks(sender: str, body: str) -> None:
    if not webhook_urls:
        return
    payload = {"sender": sender, "body": body}
    async with aiohttp.ClientSession() as session:
        for url in webhook_urls:
            try:
                await session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5))
            except Exception as exc:
                logger.warning(
                    "Webhook POST to %s failed: %s",
                    redact_url_for_log(url),
                    type(exc).__name__,
                )


async def run_server(
    config: ModemConfig,
    *,
    host: str = HTTP_HOST,
    port: int = HTTP_PORT,
    api_keys: list[str] | None = None,
) -> None:
    async with Modem(config) as modem:

        # -- Call handling: auto-answer, play greeting, hang up --
        @modem.on_call
        async def handle_call(session: CallSession) -> None:
            logger.info("Incoming call from %s — playing greeting", session.number)
            await session.play(AUDIO_GREET)
            await session.hangup()
            logger.info("Call ended")

        # -- SMS handling: store + forward to webhooks --
        async def on_sms(event: IncomingSMSEvent) -> None:
            received_messages.append({
                "sender": event.sender,
                "body": event.body,
                "received_at": event.timestamp.isoformat(),
            })
            await notify_webhooks(event.sender, event.body)

        modem.sms.on_message(on_sms)

        # -- Delivery report handling --
        async def on_delivery_report(event: SMSDeliveryReportEvent) -> None:
            delivery_reports.append({
                "recipient": event.recipient,
                "status": event.status,
                "timestamp": event.timestamp.isoformat(),
            })

        modem.bus.subscribe(SMSDeliveryReportEvent, on_delivery_report)

        # -- HTTP server --
        app = create_app(modem, api_keys=api_keys)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        logger.info(
            "HTTP server listening on %s:%d with %d configured API key(s)",
            host,
            port,
            len(api_keys or []),
        )

        try:
            await modem.run_forever()
        finally:
            await runner.cleanup()


async def main() -> None:
    await run_server(ModemConfig(), host=HTTP_HOST, port=HTTP_PORT)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(main())
