"""PII-safe in-process health and metrics helpers."""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

from callstack.events.types import (
    CallState,
    CallStateEvent,
    IncomingSMSEvent,
    ModemDisconnectedEvent,
    ModemReconnectedEvent,
    SMSDeliveryReportEvent,
    SMSSentEvent,
    SignalQualityEvent,
    USSDResponseEvent,
)

_DELIVERY_STATUSES = {"delivered", "failed", "pending"}


def modem_connected(modem: Any) -> bool:
    """Return a public-safe readiness flag for real or fake modem objects."""
    connected = getattr(modem, "connected", None)
    if connected is None:
        connected = getattr(modem, "_connected", False)
    if callable(connected):
        connected = connected()
    return bool(connected)


def sms_store_ready(modem: Any) -> bool:
    """Return whether an SMS storage object is present without exposing its path."""
    sms = getattr(modem, "sms", None)
    if sms is None:
        return False
    return getattr(sms, "store", None) is not None or getattr(sms, "_store", None) is not None


class CallstackMetrics:
    """Small dependency-free metrics collector for the HTTP adapter.

    The collector intentionally stores only aggregate counters/gauges. Event fields
    that can contain phone numbers, SMS bodies, USSD text, modem identifiers, or raw
    errors are ignored so `/metrics` remains safe for broad monitoring systems.
    """

    def __init__(self, modem: Any):
        self._modem = modem
        self._started_at = time.monotonic()
        self.sms_received_total = 0
        self.sms_sent_total = 0
        self.delivery_reports_total: Counter[str] = Counter()
        self.active_calls = 0
        self.signal_rssi: int | None = None
        self.signal_ber: int | None = None
        self.modem_disconnects_total = 0
        self.modem_reconnects_total = 0
        self.ussd_responses_total = 0
        self._subscribe(getattr(modem, "bus", None))

    @property
    def uptime_seconds(self) -> float:
        return max(0.0, time.monotonic() - self._started_at)

    def health_payload(self) -> dict[str, object]:
        connected = modem_connected(self._modem)
        return {
            "status": "ready" if connected else "degraded",
            "modem_connected": connected,
            "uptime_seconds": self.uptime_seconds,
            "sms_store_ready": sms_store_ready(self._modem),
        }

    def health_status(self) -> int:
        return 200 if modem_connected(self._modem) else 503

    def render_prometheus(self) -> str:
        lines = [
            "# HELP callstack_uptime_seconds Seconds since HTTP app startup.",
            "# TYPE callstack_uptime_seconds gauge",
            f"callstack_uptime_seconds {self.uptime_seconds:.6f}",
            "# HELP callstack_sms_received_total Total completed inbound SMS messages.",
            "# TYPE callstack_sms_received_total counter",
            f"callstack_sms_received_total {self.sms_received_total}",
            "# HELP callstack_sms_sent_total Total outbound SMS messages accepted by the modem service.",
            "# TYPE callstack_sms_sent_total counter",
            f"callstack_sms_sent_total {self.sms_sent_total}",
            "# HELP callstack_sms_delivery_reports_total Total SMS delivery reports by sanitized status.",
            "# TYPE callstack_sms_delivery_reports_total counter",
        ]
        for status in sorted(self.delivery_reports_total):
            lines.append(
                f'callstack_sms_delivery_reports_total{{status="{status}"}} {self.delivery_reports_total[status]}'
            )
        lines.extend([
            "# HELP callstack_active_calls Current active/ringing/held/dialing call count.",
            "# TYPE callstack_active_calls gauge",
            f"callstack_active_calls {self.active_calls}",
        ])
        if self.signal_rssi is not None:
            lines.extend([
                "# HELP callstack_signal_rssi Last observed signal RSSI value.",
                "# TYPE callstack_signal_rssi gauge",
                f"callstack_signal_rssi {self.signal_rssi}",
            ])
        if self.signal_ber is not None:
            lines.extend([
                "# HELP callstack_signal_ber Last observed signal BER value.",
                "# TYPE callstack_signal_ber gauge",
                f"callstack_signal_ber {self.signal_ber}",
            ])
        lines.extend([
            "# HELP callstack_modem_disconnects_total Total modem disconnect events.",
            "# TYPE callstack_modem_disconnects_total counter",
            f"callstack_modem_disconnects_total {self.modem_disconnects_total}",
            "# HELP callstack_modem_reconnects_total Total modem reconnect events.",
            "# TYPE callstack_modem_reconnects_total counter",
            f"callstack_modem_reconnects_total {self.modem_reconnects_total}",
            "# HELP callstack_ussd_responses_total Total USSD response events.",
            "# TYPE callstack_ussd_responses_total counter",
            f"callstack_ussd_responses_total {self.ussd_responses_total}",
            "",
        ])
        return "\n".join(lines)

    def _subscribe(self, bus: Any) -> None:
        if bus is None or not hasattr(bus, "subscribe"):
            return
        bus.subscribe(IncomingSMSEvent, self._on_sms_received)
        bus.subscribe(SMSSentEvent, self._on_sms_sent)
        bus.subscribe(SMSDeliveryReportEvent, self._on_delivery_report)
        bus.subscribe(CallStateEvent, self._on_call_state)
        bus.subscribe(SignalQualityEvent, self._on_signal_quality)
        bus.subscribe(ModemDisconnectedEvent, self._on_modem_disconnected)
        bus.subscribe(ModemReconnectedEvent, self._on_modem_reconnected)
        bus.subscribe(USSDResponseEvent, self._on_ussd_response)

    async def _on_sms_received(self, _event: IncomingSMSEvent) -> None:
        self.sms_received_total += 1

    async def _on_sms_sent(self, _event: SMSSentEvent) -> None:
        self.sms_sent_total += 1

    async def _on_delivery_report(self, event: SMSDeliveryReportEvent) -> None:
        self.delivery_reports_total[_safe_delivery_status(event.status)] += 1

    async def _on_call_state(self, event: CallStateEvent) -> None:
        self.active_calls = int(event.state in {CallState.DIALING, CallState.RINGING, CallState.ACTIVE, CallState.HELD})

    async def _on_signal_quality(self, event: SignalQualityEvent) -> None:
        self.signal_rssi = event.rssi
        self.signal_ber = event.ber

    async def _on_modem_disconnected(self, _event: ModemDisconnectedEvent) -> None:
        self.modem_disconnects_total += 1

    async def _on_modem_reconnected(self, _event: ModemReconnectedEvent) -> None:
        self.modem_reconnects_total += 1

    async def _on_ussd_response(self, _event: USSDResponseEvent) -> None:
        self.ussd_responses_total += 1


def _safe_delivery_status(status: str) -> str:
    normalized = status.strip().lower()
    if normalized in _DELIVERY_STATUSES:
        return normalized
    return "unknown"
