"""Tests for SMS delivery report handling."""

import logging

import pytest
from callstack.events.bus import EventBus
from callstack.events.types import (
    SMSDeliveryReportEvent,
    USSDResponseEvent,
    _RawDeliveryReport,
    _RawSMSNotification,
)
from callstack.protocol.executor import ATCommandExecutor
from callstack.protocol.parser import ATResponseParser
from callstack.protocol.urc import URCDispatcher
from callstack.sms.service import SMSService
from callstack.transport.mock import MockTransport


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


class TestDeliveryReportService:
    async def test_text_mode_report_preserves_reference_recipient_and_status(self, bus, urc):
        transport = MockTransport()
        service = SMSService(ATCommandExecutor(transport, urc), bus)

        async with bus.stream(SMSDeliveryReportEvent) as stream:
            transport.feed(
                '+CMGR: "REC READ",6,"+120****0123",145,'
                '"24/12/25,14:30:00+04","24/12/25,14:30:05+04",0',
                "OK",
                "OK",
            )
            await service._on_delivery_report(_RawDeliveryReport(storage="SM", index=5))
            event = await stream.next(timeout=1.0)

        assert event.reference == 6
        assert event.recipient == "+120****0123"
        assert event.status == "delivered"
        written_commands = [command.strip() for command in transport.all_written]
        assert "AT+CMGR=5" in written_commands
        assert "AT+CMGD=5" in written_commands

    @pytest.mark.parametrize(
        ("status_code", "expected_status"),
        [
            (1, "delivered"),
            (2, "delivered"),
            (31, "delivered"),
            (32, "pending"),
            (33, "pending"),
            (63, "pending"),
            (64, "failed"),
            (96, "failed"),
        ],
    )
    async def test_text_mode_report_maps_tp_st_status_ranges(
        self, bus, urc, status_code, expected_status
    ):
        transport = MockTransport()
        service = SMSService(ATCommandExecutor(transport, urc), bus)

        async with bus.stream(SMSDeliveryReportEvent) as stream:
            transport.feed(
                f'+CMGR: "REC READ",7,"+120****0456",145,'
                f'"24/12/25,14:30:00+04","24/12/25,14:30:05+04",{status_code}',
                "OK",
                "OK",
            )
            await service._on_delivery_report(_RawDeliveryReport(storage="SM", index=6))
            event = await stream.next(timeout=1.0)

        assert event.reference == 7
        assert event.recipient == "+120****0456"
        assert event.status == expected_status

    async def test_malformed_report_does_not_emit_false_delivery_success_or_delete_slot(self, bus, urc):
        transport = MockTransport()
        service = SMSService(ATCommandExecutor(transport, urc), bus)

        async with bus.stream(SMSDeliveryReportEvent) as stream:
            transport.feed('+CMGR: "REC READ",not-a-reference,"+120****0999"', "OK", "OK")
            await service._on_delivery_report(_RawDeliveryReport(storage="SM", index=7))
            event = await stream.next(timeout=0.01)

        assert event is None
        written_commands = [command.strip() for command in transport.all_written]
        assert "AT+CMGR=7" in written_commands
        assert "AT+CMGD=7" not in written_commands

    async def test_delivery_report_logs_status_without_recipient(self, bus, urc, caplog):
        transport = MockTransport()
        service = SMSService(ATCommandExecutor(transport, urc), bus)

        with caplog.at_level(logging.INFO, logger="callstack.sms"):
            transport.feed(
                '+CMGR: "REC READ",8,"+15550001234",145,'
                '"24/12/25,14:30:00+04","24/12/25,14:30:05+04",0',
                "OK",
                "OK",
            )
            await service._on_delivery_report(_RawDeliveryReport(storage="SM", index=8))

        assert "+15550001234" not in caplog.text
        assert "reference 8" in caplog.text
        assert "delivered" in caplog.text


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
