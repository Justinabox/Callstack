"""Tests for the URC dispatcher."""

import asyncio
import pytest
from callstack.events.bus import EventBus
from callstack.events.types import (
    CallState,
    CallStateEvent,
    CallerIDEvent,
    DTMFEvent,
    IncomingSMSEvent,
    RingEvent,
    _RawSMSNotification,
)
from callstack.protocol.urc import URCDispatcher


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def urc(bus):
    return URCDispatcher(bus)


class TestIsURC:
    def test_ring(self, urc):
        assert urc.is_urc("RING") is True

    def test_clip(self, urc):
        assert urc.is_urc('+CLIP: "+15551234567",145') is True

    def test_dtmf(self, urc):
        assert urc.is_urc("+DTMF: 5") is True

    def test_rxdtmf(self, urc):
        assert urc.is_urc("RXDTMF: 3") is True

    def test_voice_call(self, urc):
        assert urc.is_urc("VOICE CALL: BEGIN") is True

    def test_no_carrier(self, urc):
        assert urc.is_urc("NO CARRIER") is True

    def test_cmt(self, urc):
        assert urc.is_urc('+CMT: "+1555"') is True

    def test_cmti(self, urc):
        assert urc.is_urc('+CMTI: "SM",3') is True

    def test_ok_not_urc(self, urc):
        assert urc.is_urc("OK") is False

    def test_error_not_urc(self, urc):
        assert urc.is_urc("ERROR") is False

    def test_data_not_urc(self, urc):
        assert urc.is_urc("+CSQ: 20,0") is False


class TestDispatch:
    async def test_ring(self, bus, urc):
        async with bus.stream(RingEvent) as stream:
            await urc.dispatch("RING")
            event = await stream.next(timeout=1.0)
            assert isinstance(event, RingEvent)

    async def test_clip(self, bus, urc):
        async with bus.stream(CallerIDEvent) as stream:
            await urc.dispatch('+CLIP: "+15551234567",145,,,,0')
            event = await stream.next(timeout=1.0)
            assert event.number == "+15551234567"

    async def test_dtmf(self, bus, urc):
        async with bus.stream(DTMFEvent) as stream:
            await urc.dispatch("+DTMF: 5")
            event = await stream.next(timeout=1.0)
            assert event.digit == "5"

    async def test_rxdtmf(self, bus, urc):
        async with bus.stream(DTMFEvent) as stream:
            await urc.dispatch("RXDTMF: 3")
            event = await stream.next(timeout=1.0)
            assert event.digit == "3"

    async def test_voice_call_begin(self, bus, urc):
        async with bus.stream(CallStateEvent) as stream:
            await urc.dispatch("VOICE CALL: BEGIN")
            event = await stream.next(timeout=1.0)
            assert event.state == CallState.ACTIVE

    async def test_voice_call_end(self, bus, urc):
        async with bus.stream(CallStateEvent) as stream:
            await urc.dispatch("VOICE CALL: END: 00:15")
            event = await stream.next(timeout=1.0)
            assert event.state == CallState.ENDED

    async def test_no_carrier(self, bus, urc):
        async with bus.stream(CallStateEvent) as stream:
            await urc.dispatch("NO CARRIER")
            event = await stream.next(timeout=1.0)
            assert event.state == CallState.ENDED

    async def test_cmt(self, bus, urc):
        async with bus.stream(_RawSMSNotification) as stream:
            await urc.dispatch('+CMT: "+15551234567","","2024/01/15"', followup="Hello world")
            event = await stream.next(timeout=1.0)
            assert event.sender == "+15551234567"
            assert event.body == "Hello world"

    async def test_cmt_without_body(self, bus, urc):
        async with bus.stream(_RawSMSNotification) as stream:
            await urc.dispatch('+CMT: "+15551234567","","2024/01/15"')
            event = await stream.next(timeout=1.0)
            assert event.sender == "+15551234567"
            assert event.body == ""

    async def test_cmti(self, bus, urc):
        async with bus.stream(_RawSMSNotification) as stream:
            await urc.dispatch('+CMTI: "SM",3')
            event = await stream.next(timeout=1.0)
            assert isinstance(event, _RawSMSNotification)
