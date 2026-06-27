"""Tests for the NetworkService."""

import asyncio
import pytest

from callstack.errors import ATTimeoutError
from callstack.events.bus import EventBus
from callstack.events.types import SignalQualityEvent
from callstack.network import NetworkService, SignalInfo, RegistrationInfo
from callstack.protocol.executor import ATCommandExecutor
from callstack.protocol.urc import URCDispatcher
from callstack.transport.mock import MockTransport


def _make_service():
    transport = MockTransport()
    bus = EventBus()
    urc = URCDispatcher(bus)
    executor = ATCommandExecutor(transport, urc)
    service = NetworkService(executor, bus)
    return service, transport, bus


def _feed_registration_responses(
    transport,
    creg: str | None = "+CREG: 0,0",
    cgreg: str | None = "+CGREG: 0,0",
    cereg: str | None = "+CEREG: 0,0",
) -> None:
    """Queue responses for the expected CREG/CGREG/CEREG query sequence."""
    for line in (creg, cgreg, cereg):
        if line is not None:
            transport.feed(line)
        transport.feed("OK")


def _assert_registration_family_queries(transport) -> None:
    assert transport.all_written == [
        "AT+CREG?\r\n",
        "AT+CGREG?\r\n",
        "AT+CEREG?\r\n",
    ]


class TestSignalQuality:
    async def test_returns_signal_info(self):
        svc, transport, bus = _make_service()
        await transport.open()

        transport.feed("+CSQ: 18,2", "OK")
        info = await svc.signal_quality()

        assert isinstance(info, SignalInfo)
        assert info.rssi == 18
        assert info.ber == 2
        assert info.ber_description == "good"
        assert info.dbm == -77
        assert info.description == "good"

    async def test_emits_event(self):
        svc, transport, bus = _make_service()
        await transport.open()

        received = []

        async def capture(e):
            received.append(e)

        bus.subscribe(SignalQualityEvent, capture)

        transport.feed("+CSQ: 20,0", "OK")
        await svc.signal_quality()
        await asyncio.sleep(0.01)

        assert len(received) == 1
        assert received[0].rssi == 20

    async def test_unknown_signal(self):
        svc, transport, _ = _make_service()
        await transport.open()

        transport.feed("+CSQ: 99,99", "OK")
        info = await svc.signal_quality()

        assert info.rssi == 99
        assert info.dbm is None
        assert info.description == "unknown"

    async def test_no_csq_in_response(self):
        svc, transport, _ = _make_service()
        await transport.open()

        transport.feed("OK")
        info = await svc.signal_quality()

        assert info.description == "unknown"


class TestRegistration:
    async def test_uses_configured_command_timeout(self):
        class Capture:
            lines = ["+CGREG: 0,5"]

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class RecordingExecutor:
            def __init__(self):
                self.calls = []

            def capture_urcs(self, *prefixes):
                return Capture()

            async def execute(self, command, expect=("OK",), timeout=5.0):
                self.calls.append((command, timeout))

        executor = RecordingExecutor()
        svc = NetworkService(executor, EventBus(), command_timeout=2.5)

        info = await svc.registration()

        assert info.registered is True
        assert executor.calls == [
            ("AT+CREG?", 2.5),
            ("AT+CGREG?", 2.5),
            ("AT+CEREG?", 2.5),
        ]

    async def test_registered_home(self):
        svc, transport, _ = _make_service()
        await transport.open()

        _feed_registration_responses(transport, creg="+CREG: 0,1")
        info = await svc.registration()

        assert isinstance(info, RegistrationInfo)
        assert info.registered is True
        assert info.roaming is False
        assert "home" in info.description
        _assert_registration_family_queries(transport)

    async def test_roaming(self):
        svc, transport, _ = _make_service()
        await transport.open()

        _feed_registration_responses(transport, creg="+CREG: 0,5")
        info = await svc.registration()

        assert info.registered is True
        assert info.roaming is True
        _assert_registration_family_queries(transport)

    async def test_packet_registration_roaming(self):
        svc, transport, _ = _make_service()
        await transport.open()

        _feed_registration_responses(
            transport,
            creg="+CREG: 0,0",
            cgreg="+CGREG: 0,5",
            cereg="+CEREG: 0,0",
        )
        info = await svc.registration()

        assert info.registered is True
        assert info.roaming is True
        _assert_registration_family_queries(transport)

    async def test_lte_registration_home(self):
        svc, transport, _ = _make_service()
        await transport.open()

        _feed_registration_responses(
            transport,
            creg="+CREG: 0,0",
            cgreg="+CGREG: 0,0",
            cereg="+CEREG: 0,1",
        )
        info = await svc.registration()

        assert info.registered is True
        assert info.roaming is False
        _assert_registration_family_queries(transport)

    async def test_lte_registration_verbose_empty_optional_fields(self):
        svc, transport, _ = _make_service()
        await transport.open()

        _feed_registration_responses(
            transport,
            creg="+CREG: 0,0",
            cgreg="+CGREG: 0,0",
            cereg='+CEREG: 2,1,"ABCD","12345678",7,,,"00000001","00000110"',
        )
        info = await svc.registration()

        assert info.registered is True
        assert info.roaming is False
        assert info.status == 1
        assert info.mode == 2
        _assert_registration_family_queries(transport)

    async def test_packet_registration_verbose_empty_optional_fields(self):
        svc, transport, _ = _make_service()
        await transport.open()

        _feed_registration_responses(
            transport,
            creg="+CREG: 0,0",
            cgreg='+CGREG: 2,5,"ABCD","12345678",7,,',
            cereg="+CEREG: 0,0",
        )
        info = await svc.registration()

        assert info.registered is True
        assert info.roaming is True
        assert info.status == 5
        assert info.mode == 2
        _assert_registration_family_queries(transport)

    async def test_not_registered(self):
        svc, transport, _ = _make_service()
        await transport.open()

        _feed_registration_responses(
            transport,
            creg="+CREG: 0,0",
            cgreg="+CGREG: 0,2",
            cereg="+CEREG: 0,3",
        )
        info = await svc.registration()

        assert info.registered is False
        assert info.status == 0
        assert info.mode == 0

    async def test_searching(self):
        svc, transport, _ = _make_service()
        await transport.open()

        _feed_registration_responses(transport, creg="+CREG: 0,2")
        info = await svc.registration()

        assert info.registered is False
        assert "searching" in info.description

    async def test_falls_back_when_no_registration_status_parses(self):
        svc, transport, _ = _make_service()
        await transport.open()

        _feed_registration_responses(transport, creg=None, cgreg=None, cereg=None)
        info = await svc.registration()

        assert info == RegistrationInfo(status=0, mode=0)
        _assert_registration_family_queries(transport)

    async def test_optional_registration_query_timeout_preserves_captured_status(self):
        class Capture:
            lines = ["+CREG: 0,1"]

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class TimeoutAfterCircuitExecutor:
            def __init__(self):
                self.calls = []

            def capture_urcs(self, *prefixes):
                return Capture()

            async def execute(self, command, expect=("OK",), timeout=5.0):
                self.calls.append((command, timeout))
                if command == "AT+CGREG?":
                    raise ATTimeoutError("timed out waiting for packet registration")

        executor = TimeoutAfterCircuitExecutor()
        svc = NetworkService(executor, EventBus(), command_timeout=2.5)

        info = await svc.registration()

        assert info == RegistrationInfo(status=1, mode=0)
        assert executor.calls == [
            ("AT+CREG?", 2.5),
            ("AT+CGREG?", 2.5),
        ]


class TestOperator:
    async def test_returns_operator_name(self):
        svc, transport, _ = _make_service()
        await transport.open()

        transport.feed('+COPS: 0,0,"T-Mobile"', "OK")
        name = await svc.operator()

        assert name == "T-Mobile"

    async def test_no_operator(self):
        svc, transport, _ = _make_service()
        await transport.open()

        transport.feed("+COPS: 0", "OK")
        name = await svc.operator()

        assert name is None


class TestWaitForRegistration:
    async def test_already_registered(self):
        svc, transport, _ = _make_service()
        await transport.open()

        _feed_registration_responses(transport, creg="+CREG: 0,1")
        result = await svc.wait_for_registration(timeout=1.0)

        assert result is True

    async def test_timeout(self):
        svc, transport, _ = _make_service()
        await transport.open()

        # Feed "not registered" responses repeatedly
        for _ in range(10):
            _feed_registration_responses(transport)

        result = await svc.wait_for_registration(timeout=0.2, poll_interval=0.05)

        assert result is False
