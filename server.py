"""Auto-answer calls + SMS HTTP server using Callstack."""

import asyncio
import logging
from pathlib import Path

import aiohttp
from aiohttp import web

from callstack import Modem, ModemConfig, CallSession, IncomingSMSEvent

logger = logging.getLogger("server")

AUDIO_GREET = str(Path(__file__).parent / "audio" / "greet.wav")
HTTP_HOST = "0.0.0.0"
HTTP_PORT = 8080

# Webhook subscribers and received messages store
webhook_urls: list[str] = []
received_messages: list[dict] = []


def create_app(modem: Modem) -> web.Application:
    app = web.Application()

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

    app.router.add_post("/sms/send", send_sms)
    app.router.add_post("/sms/subscribe", subscribe)
    app.router.add_get("/sms/messages", list_messages)
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
            received_messages.append({"sender": event.sender, "body": event.body})
            await notify_webhooks(event.sender, event.body)

        modem.sms.on_message(on_sms)

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
