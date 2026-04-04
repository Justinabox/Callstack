"""Tests for USSD service."""

import asyncio
import pytest
from callstack.events.bus import EventBus
from callstack.events.types import USSDResponseEvent
from callstack.protocol.commands import ATCommand
from callstack.protocol.parser import ATResponseParser
from callstack.protocol.urc import URCDispatcher


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def urc(bus):
    return URCDispatcher(bus)


class TestUSSDCommand:
    def test_ussd_send(self):
        assert ATCommand.ussd_send("*100#") == 'AT+CUSD=1,"*100#",15'

    def test_ussd_send_custom_encoding(self):
        assert ATCommand.ussd_send("*100#", encoding=0) == 'AT+CUSD=1,"*100#",0'

    def test_ussd_cancel(self):
        assert ATCommand.USSD_CANCEL == "AT+CUSD=2"


class TestCUSDParser:
    def test_parse_cusd_with_message(self):
        result = ATResponseParser.parse_cusd('+CUSD: 0,"Your balance is $5.00",15')
        assert result == (0, "Your balance is $5.00", 15)

    def test_parse_cusd_further_action(self):
        result = ATResponseParser.parse_cusd('+CUSD: 1,"Select option: 1-Balance 2-Plans",15')
        assert result == (1, "Select option: 1-Balance 2-Plans", 15)

    def test_parse_cusd_terminated(self):
        result = ATResponseParser.parse_cusd('+CUSD: 2')
        assert result == (2, "", 15)

    def test_parse_cusd_no_encoding(self):
        result = ATResponseParser.parse_cusd('+CUSD: 0,"Balance: $10"')
        assert result == (0, "Balance: $10", 15)

    def test_parse_cusd_invalid(self):
        result = ATResponseParser.parse_cusd("OK")
        assert result is None


class TestCUSDDispatch:
    async def test_cusd_emits_event(self, bus, urc):
        async with bus.stream(USSDResponseEvent) as stream:
            await urc.dispatch('+CUSD: 0,"Your balance is $5.00",15')
            event = await stream.next(timeout=1.0)
            assert isinstance(event, USSDResponseEvent)
            assert event.status == 0
            assert event.message == "Your balance is $5.00"
            assert event.encoding == 15

    async def test_cusd_terminated(self, bus, urc):
        async with bus.stream(USSDResponseEvent) as stream:
            await urc.dispatch('+CUSD: 2')
            event = await stream.next(timeout=1.0)
            assert event.status == 2
            assert event.message == ""


class TestUSSDResponseEvent:
    def test_event_fields(self):
        event = USSDResponseEvent(status=0, message="Balance: $5", encoding=15)
        assert event.status == 0
        assert event.message == "Balance: $5"
        assert event.encoding == 15

    def test_defaults(self):
        event = USSDResponseEvent()
        assert event.status == 0
        assert event.message == ""
        assert event.encoding == 15
