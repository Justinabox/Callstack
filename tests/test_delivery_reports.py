"""Tests for SMS delivery report handling."""

import pytest
from callstack.events.bus import EventBus
from callstack.events.types import (
    SMSDeliveryReportEvent,
    USSDResponseEvent,
    _RawDeliveryReport,
    _RawSMSNotification,
)
from callstack.protocol.parser import ATResponseParser
from callstack.protocol.urc import URCDispatcher


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def urc(bus):
    return URCDispatcher(bus)


class TestCDSIParser:
    def test_parse_cdsi(self):
        result = ATResponseParser.parse_cdsi('+CDSI: "SM",5')
        assert result == ("SM", 5)

    def test_parse_cdsi_me_storage(self):
        result = ATResponseParser.parse_cdsi('+CDSI: "ME",12')
        assert result == ("ME", 12)

    def test_parse_cdsi_invalid(self):
        result = ATResponseParser.parse_cdsi("OK")
        assert result is None


class TestCDSIDispatch:
    async def test_cdsi_emits_raw_delivery_report(self, bus, urc):
        async with bus.stream(_RawDeliveryReport) as stream:
            await urc.dispatch('+CDSI: "SM",5')
            event = await stream.next(timeout=1.0)
            assert isinstance(event, _RawDeliveryReport)
            assert event.storage == "SM"
            assert event.index == 5


class TestDeliveryReportEvent:
    def test_event_fields(self):
        event = SMSDeliveryReportEvent(
            recipient="+15551234567",
            status="delivered",
            reference=42,
        )
        assert event.recipient == "+15551234567"
        assert event.status == "delivered"
        assert event.reference == 42

    def test_event_defaults(self):
        event = SMSDeliveryReportEvent()
        assert event.recipient == ""
        assert event.status == ""
        assert event.reference == 0
