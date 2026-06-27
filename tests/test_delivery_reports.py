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

    def test_parse_cdsi_accepts_optional_comma_whitespace(self):
        result = ATResponseParser.parse_cdsi('+CDSI: "SM", 5')
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

    async def test_cdsi_dispatch_accepts_optional_comma_whitespace(self, bus, urc):
        async with bus.stream(_RawDeliveryReport) as stream:
            await urc.dispatch('+CDSI: "SM", 5')
            event = await stream.next(timeout=1.0)
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
                '+CMGR: "REC READ",8,"+155****1234",145,'
                '"24/12/25,14:30:00+04","24/12/25,14:30:05+04",0',
                "OK",
                "OK",
            )
            await service._on_delivery_report(_RawDeliveryReport(storage="SM", index=8))

        assert "+155****1234" not in caplog.text
        assert "reference 8" in caplog.text
        assert "delivered" in caplog.text

    async def test_cleanup_failure_warns_without_recipient_after_first_emit(
        self, bus, urc, caplog
    ):
        transport = MockTransport()
        service = SMSService(ATCommandExecutor(transport, urc), bus)

        async with bus.stream(SMSDeliveryReportEvent) as stream:
            with caplog.at_level(logging.WARNING, logger="callstack.sms"):
                transport.feed(
                    '+CMGR: "REC READ",9,"+155****9999",145,'
                    '"24/12/25,14:30:00+04","24/12/25,14:30:05+04",0',
                    "OK",
                    "ERROR",
                )
                await service._on_delivery_report(_RawDeliveryReport(storage="SM", index=9))
            event = await stream.next(timeout=1.0)

        assert event is not None
        assert event.reference == 9
        assert event.status == "delivered"
        assert "Failed to delete delivery report slot after local acceptance" in caplog.text
        assert "storage=SM index=9" in caplog.text
        assert "+155****9999" not in caplog.text

    async def test_uncleared_delivery_report_retry_does_not_emit_duplicate_and_clears_marker(
        self, bus, urc
    ):
        transport = MockTransport()
        service = SMSService(ATCommandExecutor(transport, urc), bus)
        first_report = (
            '+CMGR: "REC READ",10,"+155****1010",145,'
            '"24/12/25,14:30:00+04","24/12/25,14:30:05+04",0'
        )
        reused_slot_report = (
            '+CMGR: "REC READ",11,"+155****1111",145,'
            '"24/12/25,14:30:00+04","24/12/25,14:30:05+04",0'
        )

        async with bus.stream(SMSDeliveryReportEvent) as stream:
            transport.feed(first_report, "OK", "ERROR")
            await service._on_delivery_report(_RawDeliveryReport(storage="SM", index=10))
            first_event = await stream.next(timeout=1.0)

            transport.feed(first_report, "OK", "OK")
            await service._on_delivery_report(_RawDeliveryReport(storage="SM", index=10))
            duplicate_event = await stream.next(timeout=0.01)

            transport.feed(reused_slot_report, "OK", "OK")
            await service._on_delivery_report(_RawDeliveryReport(storage="SM", index=10))
            reused_slot_event = await stream.next(timeout=1.0)

        assert first_event is not None
        assert first_event.reference == 10
        assert duplicate_event is None
        assert reused_slot_event is not None
        assert reused_slot_event.reference == 11
        written_commands = [command.strip() for command in transport.all_written]
        assert written_commands.count("AT+CMGR=10") == 3
        assert written_commands.count("AT+CMGD=10") == 3

    async def test_successful_cleanup_of_different_report_clears_stale_marker(
        self, bus, urc
    ):
        transport = MockTransport()
        service = SMSService(ATCommandExecutor(transport, urc), bus)
        stale_report = (
            '+CMGR: "REC READ",12,"+155****1212",145,'
            '"24/12/25,14:30:00+04","24/12/25,14:30:05+04",0'
        )
        replacement_report = (
            '+CMGR: "REC READ",13,"+155****1313",145,'
            '"24/12/25,14:30:00+04","24/12/25,14:30:05+04",0'
        )

        async with bus.stream(SMSDeliveryReportEvent) as stream:
            transport.feed(stale_report, "OK", "ERROR")
            await service._on_delivery_report(_RawDeliveryReport(storage="SM", index=12))
            stale_event = await stream.next(timeout=1.0)

            transport.feed(replacement_report, "OK", "OK")
            await service._on_delivery_report(_RawDeliveryReport(storage="SM", index=12))
            replacement_event = await stream.next(timeout=1.0)

            transport.feed(stale_report, "OK", "OK")
            await service._on_delivery_report(_RawDeliveryReport(storage="SM", index=12))
            later_stale_fingerprint_event = await stream.next(timeout=1.0)

        assert stale_event is not None
        assert stale_event.reference == 12
        assert replacement_event is not None
        assert replacement_event.reference == 13
        assert later_stale_fingerprint_event is not None
        assert later_stale_fingerprint_event.reference == 12


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
