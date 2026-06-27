# Callstack 🎙️📡

**Async-first GSM/LTE modem telephony framework for Raspberry Pi.**

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Callstack provides a high-level Python API for managing GSM/LTE modem connections on Raspberry Pi. Built on `asyncio` with proper state machines, typed events, and clean separation of concerns — it handles voice calls, SMS, and raw AT commands without the thread-per-feature sprawl.

---

## ✨ Features

| Feature | Status | Notes |
|---------|--------|-------|
| **Voice Calls** | ✅ Ready | Inbound/outbound, recording, tone playback, IVR menus, DTMF send/collect |
| **SMS** | ✅ Ready | Send/receive/subscribe, SQLite persistence, delivery reports, multipart UDH metadata; full multipart reassembly is planned |
| **SIM + Network** | ✅ Ready | SIM PIN unlock, registration/signal snapshots, BER descriptions |
| **USSD** | ✅ Ready | `AT+CUSD` balance checks/carrier menus via service + HTTP endpoint |
| **Raw AT Commands** | ✅ Ready | Direct modem control via `Modem.execute()` |
| **HTTP Server** | ✅ Ready | API-key auth, rate limiting, SMS/USSD/delivery-report endpoints, `/healthz`, and PII-safe `/metrics` |
| **CLI** | ✅ Partial | `callstack status`, `callstack send`, safe `callstack doctor`, and PII-safe `callstack monitor`; packaged `callstack serve` is planned |
| **Auto-reconnect** | ✅ Ready | Handles USB disconnect/reconnect gracefully; active multi-port auto-detection is planned |

---

## 🚀 Quick Start

### Hardware Requirements

- Raspberry Pi (3B+/4/5 recommended)
- GSM/LTE modem with USB serial (tested with SIMCOM SIM868)
- Active SIM card with SMS capability
- USB ports for modem (typically creates `/dev/ttyUSB2` and `/dev/ttyUSB4`)

### Installation

```bash
git clone https://github.com/Justinabox/Callstack.git
cd Callstack
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[sqlite]"
```

### Basic Usage

SMS examples below use `5551234` as a dummy local test recipient; replace it only with a controlled test number when running against real hardware, and never publish real SIM or customer numbers in docs, logs, issues, or PRs.

```python
import asyncio
from callstack import Modem, ModemConfig

async def main():
    async with Modem(ModemConfig()) as modem:
        # Send an SMS
        sms = await modem.sms.send("5551234", "Hello from Callstack!")
        print(f"Sent! Reference: {sms.reference}")
        
        # Subscribe to incoming messages
        @modem.sms.on_message
        async def on_sms(msg):
            print(f"From {msg.sender}: {msg.body}")
        
        # Keep running
        await modem.run_forever()

asyncio.run(main())
```

### HTTP Server Mode

Install the HTTP server runtime dependencies with the server extra:

```bash
pip install -e ".[server,sqlite]"
```

Run the built-in HTTP server for external integrations:

```bash
python server.py
```

Endpoints:
- `POST /sms/send` — Send SMS (`{"to": "5551234", "body": "..."}`); `to` must be an optional leading `+` followed by 3-15 digits in real requests (use redacted values only in public docs/logs)
- `POST /sms/subscribe` — Register webhook for incoming SMS
- `GET /sms/messages` — List received messages
- `GET /sms/delivery-reports` — List delivery status reports
- `POST /ussd/send` — Send USSD short codes (`{"code": "*123#"}`)
- `GET /healthz` — Return a public-safe readiness payload with modem connectivity, uptime, and SMS-store readiness
- `GET /metrics` — Return Prometheus text metrics with aggregate counters/gauges only; labels and values intentionally avoid phone numbers, SMS bodies, USSD payloads, SIM identifiers, API keys, and raw modem identifiers

If `create_app(..., api_keys=[...])` is configured, HTTP requests must include an `Authorization` header containing the configured bearer token, and requests are rate-limited per key. Do not expose the HTTP server beyond localhost without API keys or an equivalent trusted network boundary; deployment-safe auth defaults remain tracked separately in issue #4.

### CLI

The package exposes a `callstack` command for local Raspberry Pi workflows:

```bash
callstack status --json
callstack send --to 5551234 --body "Hello from Callstack"
callstack doctor --ports /dev/ttyUSB2,/dev/ttyUSB3 --json
callstack monitor --events sms.received,sms.delivery_report --json
```

- `callstack status` connects to the configured modem and prints registration, operator, and signal details.
- `callstack send` sends one SMS through the configured modem and prints only the modem reference.
- `callstack doctor` is the safest first hardware bring-up command. It probes only explicit candidate ports with non-mutating identity/attention commands and avoids SMS, USSD, call, SIM unlock, storage, IMEI, IMSI, ICCID, or SIM-number commands.
- `callstack monitor` tails selected typed events as sanitized human text or one JSON object per event. It uses PII-safe event serializers by default and reports queue overflow without printing phone numbers, SMS bodies, USSD payloads, webhook URLs, SIM identifiers, API keys, modem serials, or raw AT lines.

Planned CLI follow-ups include a packaged `callstack serve` HTTP-server entrypoint, active modem scan/config preview, and richer environment/config helpers for server and CLI deployments.

Voice-call DTMF sends use `AT+VTS`; `CallSession.send_dtmf(..., duration_ms=...)` encodes non-zero tone durations in 100 ms increments (for example, `300` ms becomes an `AT+VTS` duration of `3`). Use `inter_digit_delay_ms` separately when a modem or remote IVR needs spacing between tones.

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Application Layer                     │
│     User code, IVR scripts, webhook integrations        │
├─────────────────────────────────────────────────────────┤
│                     Service Layer                        │
│   CallService  │  SMSService  │  NetworkService         │
├─────────────────────────────────────────────────────────┤
│                      Protocol Layer                      │
│   ATCommandExecutor  │  ATResponseParser  │  URC        │
├─────────────────────────────────────────────────────────┤
│                      Transport Layer                     │
│   SerialTransport  │  MockTransport  (asyncio streams)   │
├─────────────────────────────────────────────────────────┤
│                      Hardware / OS                       │
│   /dev/ttyUSB2  │  /dev/ttyUSB4  │  USB modeswitch      │
└─────────────────────────────────────────────────────────┘
```

---

## 📡 Real-World Integration: Duo MFA Automation

Callstack shines when paired with browser automation for MFA flows. See [CourseXScrapper](https://github.com/Justinabox/CourseXScrapper) for a complete example that uses Callstack to automatically receive Duo SMS verification codes for USC SSO authentication.

**The flow:**
1. Browser automation hits USC SSO → Duo challenge
2. Selects "Text message passcode" on Duo
3. Duo sends SMS to Pi's modem number
4. Callstack receives it via HTTP API
5. Code is extracted and auto-submitted
6. **Zero manual steps, fully automated**

---

## 🛠️ Configuration

```python
from callstack import Modem, ModemConfig

config = ModemConfig(
    at_port="/dev/ttyUSB2",      # AT command port
    audio_port="/dev/ttyUSB4",   # PCM audio port (voice calls)
    baudrate=115200,
    command_timeout=5.0,          # Base AT command timeout
    sms_prompt_timeout=10.0,      # Wait for AT+CMGS ">" prompt
    sms_submit_timeout=30.0,      # Wait for carrier +CMGS/OK after body
    sim_pin=None,                # Optional: unlock SIMs that boot PIN-locked
    auto_reconnect=True,
    reconnect_interval=5.0,
)

async with Modem(config) as modem:
    ...
```

---

## 🧪 Testing

```bash
pytest tests/
```

Mock transport included for testing without hardware:

```python
from callstack.transport.mock import MockTransport
```

---

## 🤝 Integration Pattern: HTTP API Polling

For external services that need to consume SMS (like MFA automation):

```python
import requests
import re

class CallstackSMSClient:
    def __init__(self, api_url: str):
        self.api_url = api_url
    
    def wait_for_code(self, timeout: int = 60) -> str | None:
        """Poll for 6-8 digit passcode."""
        # Implementation: GET /sms/messages, extract \d{6,8}
        ...
```

See `modules/auth/_sms_otp.py` in CourseXScrapper for production implementation.

---

## 📚 Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — Deep dive into design patterns
- [server.py](server.py) — HTTP API reference implementation

---

## 🔧 Troubleshooting

Start with the safe doctor probe before checking live status. It only sends
non-mutating identity/attention commands (`AT`, `ATI`, `AT+GMI`, `AT+GMM`,
`AT+GMR`) to explicit ports you provide, and does not send SMS, USSD, call, SIM
unlock, storage, IMEI, IMSI, ICCID, or SIM-number commands.

```bash
callstack doctor
callstack doctor --ports /dev/ttyUSB2,/dev/ttyUSB3
callstack doctor --ports /dev/ttyUSB2 --json
```

Review the reported AT port, confidence, manufacturer/model, capabilities, and
notes before running `callstack status`.

### Modem not responding
- Check USB ports: `ls /dev/ttyUSB*`
- Verify dialout group: `groups $USER`
- Try minicom: `minicom -D /dev/ttyUSB2`

### SMS prompt hangs
Fixed in v0.1.1: Modems send `> ` without newline — `readline()` now handles this.

### Port permissions
```bash
sudo usermod -a -G dialout $USER
# Log out and back in
```

---

## 📜 License

MIT License — see LICENSE file.

---

## 🙏 Acknowledgments

Built for automating the annoying parts of academic life. If this saves you from manually entering Duo codes 50 times, it was worth it.

*Made with ❤️ by Justinabox*
