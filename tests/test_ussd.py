"""Tests for USSD service."""

import asyncio
import logging
from typing import cast

import pytest
from callstack.events.bus import EventBus
from callstack.events.types import USSDResponseEvent
from callstack.protocol.commands import ATCommand
from callstack.protocol.executor import ATCommandExecutor
from callstack.protocol.parser import ATResponseParser
from callstack.protocol.urc import URCDispatcher
from callstack.ussd import USSDService


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

    @pytest.mark.parametrize("code", ['*100#"', "*100#\rAT+CMGD=1,4", "*100#\nAT+CMGD=1,4"])
    def test_ussd_send_rejects_command_breakout_characters(self, code):
        with pytest.raises(ValueError, match="Invalid USSD code"):
            ATCommand.ussd_send(code)

    @pytest.mark.parametrize("encoding", [-1, 256, "15", True])
    def test_ussd_send_rejects_unsupported_encoding_values(self, encoding):
        with pytest.raises(ValueError, match="Invalid USSD encoding"):
            ATCommand.ussd_send("*100#", encoding=encoding)

    def test_ussd_cancel(self):
        assert ATCommand.USSD_CANCEL == "AT+CUSD=2"


class TestUSSDServiceValidation:
    async def test_send_and_cancel_use_configured_command_timeout(self, bus):
        class RecordingExecutor:
            def __init__(self):
                self.calls = []

            async def execute(self, command, expect=("OK",), timeout=5.0):
                self.calls.append((command, timeout))
                await bus.emit(USSDResponseEvent(status=0, message="balance", encoding=15))
                return type("Response", (), {"success": True, "lines": ["OK"]})()

        executor = RecordingExecutor()
        service = USSDService(cast(ATCommandExecutor, executor), bus, command_timeout=1.75)

        await service.send("*100#", timeout=0.1)
        await service.cancel()

        assert executor.calls == [
            ('AT+CUSD=1,"*100#",15', 1.75),
            ("AT+CUSD=2", 1.75),
        ]

    async def test_invalid_ussd_code_fails_before_modem_write(self, bus):
        class FailingExecutor:
            async def execute(self, *_args, **_kwargs):
                raise AssertionError("USSD validation should run before modem writes")

        service = USSDService(cast(ATCommandExecutor, FailingExecutor()), bus)

        with pytest.raises(ValueError, match="Invalid USSD code"):
            await service.send("*100#\rAT+CMGD=1,4", timeout=0.01)

    async def test_ussd_timeout_error_does_not_echo_code(self, bus):
        class SuccessfulExecutor:
            async def execute(self, *_args, **_kwargs):
                return type("Response", (), {"success": True})()

        service = USSDService(cast(ATCommandExecutor, SuccessfulExecutor()), bus)

        private_code = "*123*9999#"
        with pytest.raises(TimeoutError) as excinfo:
            await service.send(private_code, timeout=0.001)

        assert private_code not in str(excinfo.value)
        assert "USSD response" in str(excinfo.value)


class TestCUSDParser:
    def test_parse_cusd_with_message(self):
        result = ATResponseParser.parse_cusd('+CUSD: 0,"Your balance is $5.00",15')
        assert result == (0, "Your balance is $5.00", 15)

    def test_parse_cusd_accepts_optional_comma_whitespace(self):
        result = ATResponseParser.parse_cusd('+CUSD: 0, "Balance: 12", 15')
        assert result == (0, "Balance: 12", 15)

    def test_parse_cusd_rejects_unparsed_suffix(self):
        result = ATResponseParser.parse_cusd('+CUSD: 0, "Balance: 12", bad')
        assert result is None

    def test_parse_cusd_further_action(self):
        result = ATResponseParser.parse_cusd('+CUSD: 1,"Select option: 1-Balance 2-Plans",15')
        assert result == (1, "Select option: 1-Balance 2-Plans", 15)

    def test_parse_cusd_terminated(self):
        result = ATResponseParser.parse_cusd('+CUSD: 2')
        assert result == (2, "", 15)

    def test_parse_cusd_no_encoding(self):
        result = ATResponseParser.parse_cusd('+CUSD: 0,"Balance: $10"')
        assert result == (0, "Balance: $10", 15)

    def test_parse_cusd_decodes_ucs2_message(self):
        result = ATResponseParser.parse_cusd(
            '+CUSD: 0,"0059006F00750072002000620061006C0061006E00630065002000690073002000240035002E00300030",72'
        )
        assert result == (0, "Your balance is $5.00", 72)

    def test_parse_cusd_malformed_ucs2_falls_back_to_raw_message(self):
        result = ATResponseParser.parse_cusd('+CUSD: 0,"0059006",72')
        assert result == (0, "0059006", 72)

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

    async def test_cusd_dispatch_accepts_optional_comma_whitespace(self, bus, urc):
        async with bus.stream(USSDResponseEvent) as stream:
            await urc.dispatch('+CUSD: 0, "Balance: 12", 15')
            event = await stream.next(timeout=1.0)
            assert event.status == 0
            assert event.message == "Balance: 12"
            assert event.encoding == 15

    async def test_cusd_parse_failure_log_omits_response_payload(self, bus, urc, caplog):
        private_response = '+CUSD: 0, "private balance is $100", bad'

        with caplog.at_level(logging.WARNING, logger="callstack.urc"):
            async with bus.stream(USSDResponseEvent) as stream:
                await urc.dispatch(private_response)
                event = await stream.next(timeout=0.05)

        assert event is None
        assert "Could not parse USSD response" in caplog.text
        assert private_response not in caplog.text
        assert "private balance" not in caplog.text
        assert "$100" not in caplog.text

    async def test_cusd_debug_log_omits_response_payload(self, bus, urc, caplog):
        private_response = '+CUSD: 0, "private balance is $100", bad'

        with caplog.at_level(logging.DEBUG, logger="callstack.urc"):
            await urc.dispatch(private_response)

        assert "URC: +CUSD:<redacted>" in caplog.text
        assert private_response not in caplog.text
        assert "private balance" not in caplog.text
        assert "$100" not in caplog.text

    async def test_cusd_terminated(self, bus, urc):
        async with bus.stream(USSDResponseEvent) as stream:
            await urc.dispatch('+CUSD: 2')
            event = await stream.next(timeout=1.0)
            assert event.status == 2
            assert event.message == ""

    async def test_cusd_emits_decoded_ucs2_event(self, bus, urc):
        async with bus.stream(USSDResponseEvent) as stream:
            await urc.dispatch(
                '+CUSD: 0,"0059006F00750072002000620061006C0061006E00630065002000690073002000240035002E00300030",72'
            )
            event = await stream.next(timeout=1.0)
            assert event.status == 0
            assert event.message == "Your balance is $5.00"
            assert event.encoding == 72


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
