"""Tests for USSD service."""

import asyncio
import logging
from typing import cast

import pytest
from callstack.errors import ATTimeoutError
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


async def _wait_for_call_count(calls: list[str], expected: int) -> None:
    while len(calls) < expected:
        await asyncio.sleep(0)


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

    async def test_at_command_timeout_error_does_not_echo_code(self, bus):
        class TimeoutExecutor:
            async def execute(self, command, **_kwargs):
                raise ATTimeoutError(f"timed out waiting for command: {command}")

        service = USSDService(cast(ATCommandExecutor, TimeoutExecutor()), bus)

        private_code = "*123*9999#"
        with pytest.raises(ATTimeoutError) as excinfo:
            await service.send(private_code, timeout=0.1)

        assert private_code not in str(excinfo.value)
        assert "USSD command timed out" in str(excinfo.value)

    async def test_concurrent_send_after_timeout_fails_closed_without_modem_write(self, bus):
        class RecordingExecutor:
            def __init__(self):
                self.calls = []

            async def execute(self, command, expect=("OK",), timeout=5.0):
                self.calls.append(command)
                return type("Response", (), {"success": True, "lines": ["OK"]})()

        executor = RecordingExecutor()
        service = USSDService(cast(ATCommandExecutor, executor), bus)

        first = asyncio.create_task(service.send("*111#", timeout=0.001))
        await asyncio.wait_for(_wait_for_call_count(executor.calls, 1), timeout=0.1)
        second = asyncio.create_task(service.send("*222#", timeout=0.2))

        with pytest.raises(TimeoutError):
            await first
        await bus.emit(USSDResponseEvent(status=0, message="late first response", encoding=15))

        with pytest.raises(RuntimeError, match="Previous USSD request did not complete"):
            await second
        assert executor.calls == ['AT+CUSD=1,"*111#",15']

    async def test_cancel_after_timeout_keeps_next_send_fail_closed_without_modem_write(self, bus):
        class RecordingExecutor:
            def __init__(self):
                self.calls = []

            async def execute(self, command, expect=("OK",), timeout=5.0):
                self.calls.append(command)
                return type("Response", (), {"success": True, "lines": ["OK"]})()

        executor = RecordingExecutor()
        service = USSDService(cast(ATCommandExecutor, executor), bus)

        with pytest.raises(TimeoutError):
            await service.send("*111#", timeout=0.001)
        await service.cancel()
        await bus.emit(USSDResponseEvent(status=0, message="late first response", encoding=15))

        with pytest.raises(RuntimeError, match="Previous USSD request did not complete"):
            await service.send("*222#", timeout=0.2)
        assert executor.calls == [
            'AT+CUSD=1,"*111#",15',
            "AT+CUSD=2",
        ]

    async def test_cancelled_send_after_modem_write_fails_closed_without_next_modem_write(self, bus):
        class RecordingExecutor:
            def __init__(self):
                self.calls = []

            async def execute(self, command, expect=("OK",), timeout=5.0):
                self.calls.append(command)
                return type("Response", (), {"success": True, "lines": ["OK"]})()

        executor = RecordingExecutor()
        service = USSDService(cast(ATCommandExecutor, executor), bus)

        first = asyncio.create_task(service.send("*111#", timeout=1.0))
        await asyncio.wait_for(_wait_for_call_count(executor.calls, 1), timeout=0.1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await bus.emit(USSDResponseEvent(status=0, message="late first response", encoding=15))

        with pytest.raises(RuntimeError, match="Previous USSD request did not complete"):
            await service.send("*222#", timeout=0.2)
        assert executor.calls == ['AT+CUSD=1,"*111#",15']

    async def test_fail_closed_state_is_shared_across_ussd_services_on_same_event_bus(self, bus):
        class RecordingExecutor:
            def __init__(self):
                self.calls = []

            async def execute(self, command, expect=("OK",), timeout=5.0):
                self.calls.append(command)
                return type("Response", (), {"success": True, "lines": ["OK"]})()

        first_executor = RecordingExecutor()
        first_service = USSDService(cast(ATCommandExecutor, first_executor), bus)
        with pytest.raises(TimeoutError):
            await first_service.send("*111#", timeout=0.001)

        second_executor = RecordingExecutor()
        second_service = USSDService(cast(ATCommandExecutor, second_executor), bus)
        await bus.emit(USSDResponseEvent(status=0, message="late first response", encoding=15))

        with pytest.raises(RuntimeError, match="Previous USSD request did not complete"):
            await second_service.send("*222#", timeout=0.2)
        assert first_executor.calls == ['AT+CUSD=1,"*111#",15']
        assert second_executor.calls == []

    async def test_concurrent_sends_across_services_on_same_bus_are_serialized(self, bus):
        class RecordingExecutor:
            def __init__(self):
                self.calls = []

            async def execute(self, command, expect=("OK",), timeout=5.0):
                self.calls.append(command)
                return type("Response", (), {"success": True, "lines": ["OK"]})()

        first_executor = RecordingExecutor()
        second_executor = RecordingExecutor()
        first_service = USSDService(cast(ATCommandExecutor, first_executor), bus)
        second_service = USSDService(cast(ATCommandExecutor, second_executor), bus)

        first = asyncio.create_task(first_service.send("*111#", timeout=1.0))
        await asyncio.wait_for(_wait_for_call_count(first_executor.calls, 1), timeout=0.1)
        second = asyncio.create_task(second_service.send("*222#", timeout=1.0))
        for _ in range(5):
            await asyncio.sleep(0)

        assert second_executor.calls == []
        await bus.emit(USSDResponseEvent(status=0, message="first response", encoding=15))
        assert (await first).message == "first response"

        await asyncio.wait_for(_wait_for_call_count(second_executor.calls, 1), timeout=0.1)
        await bus.emit(USSDResponseEvent(status=0, message="second response", encoding=15))
        assert (await second).message == "second response"

    async def test_cancel_during_active_send_marks_session_fail_closed(self, bus):
        class RecordingExecutor:
            def __init__(self):
                self.calls = []

            async def execute(self, command, expect=("OK",), timeout=5.0):
                self.calls.append(command)
                return type("Response", (), {"success": True, "lines": ["OK"]})()

        executor = RecordingExecutor()
        service = USSDService(cast(ATCommandExecutor, executor), bus)

        pending = asyncio.create_task(service.send("*111#", timeout=1.0))
        await asyncio.wait_for(_wait_for_call_count(executor.calls, 1), timeout=0.1)
        await service.cancel()
        await bus.emit(USSDResponseEvent(status=2, message="", encoding=15))
        assert (await pending).status == 2

        with pytest.raises(RuntimeError, match="Previous USSD request did not complete"):
            await service.send("*222#", timeout=0.2)
        assert executor.calls == ['AT+CUSD=1,"*111#",15', "AT+CUSD=2"]

    async def test_concurrent_sends_are_serialized_to_avoid_response_cross_correlation(self, bus):
        class RecordingExecutor:
            def __init__(self):
                self.calls = []

            async def execute(self, command, expect=("OK",), timeout=5.0):
                self.calls.append(command)
                return type("Response", (), {"success": True, "lines": ["OK"]})()

        executor = RecordingExecutor()
        service = USSDService(cast(ATCommandExecutor, executor), bus)

        first = asyncio.create_task(service.send("*100#", timeout=1.0))
        await asyncio.wait_for(_wait_for_call_count(executor.calls, 1), timeout=0.1)

        second = asyncio.create_task(service.send("*200#", timeout=1.0))
        for _ in range(5):
            await asyncio.sleep(0)

        assert executor.calls == ['AT+CUSD=1,"*100#",15']

        await bus.emit(USSDResponseEvent(status=0, message="first response", encoding=15))
        assert (await first).message == "first response"

        await asyncio.wait_for(_wait_for_call_count(executor.calls, 2), timeout=0.1)
        assert executor.calls == ['AT+CUSD=1,"*100#",15', 'AT+CUSD=1,"*200#",15']

        await bus.emit(USSDResponseEvent(status=0, message="second response", encoding=15))
        assert (await second).message == "second response"


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

    async def test_cusd_debug_log_redacts_response_text(self, bus, urc, caplog):
        private_text = "Balance secret account 1234"

        async with bus.stream(USSDResponseEvent) as stream:
            with caplog.at_level(logging.DEBUG, logger="callstack.urc"):
                await urc.dispatch(f'+CUSD: 0,"{private_text}",15')
            event = await stream.next(timeout=1.0)

        assert event.message == private_text
        assert private_text not in caplog.text
        assert "URC: +CUSD: <redacted>" in caplog.text

    async def test_malformed_cusd_warning_log_redacts_response_text(self, urc, caplog):
        private_text = "Balance secret account 1234"

        with caplog.at_level(logging.WARNING, logger="callstack.urc"):
            await urc.dispatch(f'+CUSD: broken "{private_text}"')

        assert private_text not in caplog.text
        assert "Could not parse USSD response: +CUSD: <redacted>" in caplog.text


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
