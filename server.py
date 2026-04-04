"""Auto-answer calls + SMS HTTP server using Callstack."""

import asyncio
import logging
import secrets
import time
from collections import defaultdict
from pathlib import Path

import aiohttp
from aiohttp import web

from callstack import Modem, ModemConfig, CallSession, IncomingSMSEvent
from callstack.events.types import SMSDeliveryReportEvent

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
        self._keys: set[str] = set(api_keys) if api_keys else set()
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
        if not secrets.compare_digest(key, key) or key not in self._keys:
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

    def add_key(self, key: str) -> None:
        self._keys.add(key)
        self.enabled = True

    def revoke_key(self, key: str) -> None:
        self._keys.discard(key)
        self.enabled = bool(self._keys)


def create_app(modem: Modem, api_keys: list[str] | None = None) -> web.Application:
    auth = APIKeyAuth(api_keys=api_keys)
    app = web.Application(middlewares=[auth.middleware])

    async def send_sms(request: web.Request) -> web.Response:
        data = await request.json()
        to = data.get("to")
        body = data.get("body")
        if not to or not body:
            return web.json_response({"error": "missing 'to' or 'body'"}, status=400)
        sms = await modem.sms.send(to, body)
        return web.json_response({
            "status": "sent",
            "to": sms.recipient,
            "reference": sms.reference,
        })

    async def subscribe(request: web.Request) -> web.Response:
        data = await request.json()
        url = data.get("url")
        if not url:
            return web.json_response({"error": "missing 'url'"}, status=400)
        webhook_urls.append(url)
        return web.json_response({"status": "subscribed", "url": url})

    async def list_messages(request: web.Request) -> web.Response:
        return web.json_response(received_messages)

    async def list_delivery_reports(request: web.Request) -> web.Response:
        return web.json_response(delivery_reports)

    async def ussd_send(request: web.Request) -> web.Response:
        data = await request.json()
        code = data.get("code")
        if not code:
            return web.json_response({"error": "missing 'code'"}, status=400)
        try:
            resp = await modem.ussd.send(code, timeout=data.get("timeout", 15.0))
            return web.json_response({
                "status": resp.status,
                "message": resp.message,
            })
        except (TimeoutError, RuntimeError) as exc:
            return web.json_response({"error": str(exc)}, status=504)

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
                logger.warning("Webhook POST to %s failed: %s", url, exc)


async def main() -> None:
    async with Modem(ModemConfig()) as modem:

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
        app = create_app(modem)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, HTTP_HOST, HTTP_PORT)
        await site.start()
        logger.info("HTTP server listening on %s:%d", HTTP_HOST, HTTP_PORT)

        try:
            await modem.run_forever()
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(main())
