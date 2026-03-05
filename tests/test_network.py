"""Tests for the NetworkService."""

import asyncio
import pytest

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


class TestSignalQuality:
    async def test_returns_signal_info(self):
        svc, transport, bus = _make_service()
        await transport.open()

        transport.feed("+CSQ: 18,2", "OK")
        info = await svc.signal_quality()

        assert isinstance(info, SignalInfo)
        assert info.rssi == 18
        assert info.ber == 2
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
    async def test_registered_home(self):
        svc, transport, _ = _make_service()
        await transport.open()

        transport.feed("+CREG: 0,1", "OK")
        info = await svc.registration()

        assert isinstance(info, RegistrationInfo)
        assert info.registered is True
        assert info.roaming is False
        assert "home" in info.description

    async def test_roaming(self):
        svc, transport, _ = _make_service()
        await transport.open()

        transport.feed("+CREG: 0,5", "OK")
        info = await svc.registration()

        assert info.registered is True
        assert info.roaming is True

    async def test_not_registered(self):
        svc, transport, _ = _make_service()
        await transport.open()

        transport.feed("+CREG: 0,0", "OK")
        info = await svc.registration()

        assert info.registered is False

    async def test_searching(self):
        svc, transport, _ = _make_service()
        await transport.open()

        transport.feed("+CREG: 0,2", "OK")
        info = await svc.registration()

        assert info.registered is False
        assert "searching" in info.description


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

        transport.feed("+CREG: 0,1", "OK")
        result = await svc.wait_for_registration(timeout=1.0)

        assert result is True

    async def test_timeout(self):
        svc, transport, _ = _make_service()
        await transport.open()

        # Feed "not registered" responses repeatedly
        for _ in range(10):
            transport.feed("+CREG: 0,0", "OK")

        result = await svc.wait_for_registration(timeout=0.2, poll_interval=0.05)

        assert result is False
