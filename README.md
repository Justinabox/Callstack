# Callstack 🎙️📡

**Async-first GSM/LTE modem telephony framework for Raspberry Pi.**

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Callstack provides a high-level Python API for managing GSM/LTE modem connections on Raspberry Pi. Built on `asyncio` with proper state machines, typed events, and clean separation of concerns — it handles voice calls, SMS, and raw AT commands without the thread-per-feature sprawl.

---

## ✨ Features

| Feature | Status | Notes |
|---------|--------|-------|
| **Voice Calls** | ✅ Ready | Inbound/outbound, recording, tone playback, IVR menus |
| **SMS** | ✅ Ready | Send/receive/subscribe, SQLite persistence, HTTP API |
| **Raw AT Commands** | ✅ Ready | Direct modem control via `Modem.execute()` |
| **HTTP Server** | ✅ Ready | REST API for SMS send/receive with webhooks |
| **Auto-reconnect** | ✅ Ready | Handles USB disconnect/reconnect gracefully |

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

```python
import asyncio
from callstack import Modem, ModemConfig

async def main():
    async with Modem(ModemConfig()) as modem:
        # Send an SMS
        sms = await modem.sms.send("+1234567890", "Hello from Callstack!")
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

Run the built-in HTTP server for external integrations:

```bash
python server.py
```

Endpoints:
- `POST /sms/send` — Send SMS (`{"to": "+123...", "body": "..."}`)
- `POST /sms/subscribe` — Register webhook for incoming SMS
- `GET /sms/messages` — List received messages

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
