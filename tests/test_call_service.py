"""Tests for CallService and CallSession."""

import asyncio
import logging
import wave
from typing import cast
import pytest

from callstack.transport.mock import MockTransport
from callstack.events.bus import EventBus
from callstack.events.types import (
    CallState,
    CallStateEvent,
    CallerIDEvent,
    DTMFEvent,
    RingEvent,
)
from callstack.errors import DialError, AnswerError, ATTimeoutError, ATCommandError
from callstack.protocol.urc import URCDispatcher
from callstack.protocol.executor import ATCommandExecutor, ATResponse
from callstack.voice.audio import AudioPipeline
from callstack.voice.service import CallService, CallSession


def _make_wav(path: str, num_frames: int = 320) -> str:
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        wf.writeframes(b"\x00" * num_frames * 2)
    return path


async def _wait_for_dtmf_stream(bus: EventBus) -> None:
    async def wait_until_stream_opens() -> None:
        while bus._queues[DTMFEvent] == []:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_until_stream_opens(), timeout=0.1)


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def at_transport():
    return MockTransport()


@pytest.fixture
def audio_transport():
    return MockTransport()


@pytest.fixture
def urc(bus):
    return URCDispatcher(bus)


@pytest.fixture
def executor(at_transport, urc):
    return ATCommandExecutor(at_transport, urc)


@pytest.fixture
def audio(audio_transport, bus):
    return AudioPipeline(audio_transport, bus)


@pytest.fixture
def service(executor, audio, bus):
    return CallService(executor, audio, bus)


class TestCallService:

    async def test_initial_state(self, service):
        assert service.state == CallState.IDLE
        assert service.active_call is None

    async def test_control_commands_use_configured_command_timeout(self, bus):
        class RecordingExecutor:
            def __init__(self):
                self.calls = []

            async def execute(self, command, expect=("OK",), timeout=5.0):
                self.calls.append((command, timeout))
                return ATResponse(success=True, lines=["OK"])

        class FakeAudio:
            running = False

            async def start(self):
                self.running = True

            async def stop(self):
                self.running = False

        executor = RecordingExecutor()
        audio = FakeAudio()
        service = CallService(executor, audio, bus, command_timeout=1.75)

        await service.hangup()
        await service.reject()
        await service._fsm.transition(CallState.RINGING)
        await service._fsm.transition(CallState.ACTIVE)
        await service._ensure_audio_enabled()
        await service._disable_audio()
        session = CallSession(number="unknown", direction="inbound", service=service)
        service._active_call = session
        await session.send_dtmf("5")

        assert executor.calls == [
            ("ATH", 1.75),
            ("ATH", 1.75),
            ("AT+CPCMREG=1", 1.75),
            ("AT+CPCMREG=0", 1.75),
            ("AT+VTS=5,1", 1.75),
        ]

    async def test_dial_success(self, service, at_transport):
        # Queue the modem response
        async def respond():
            await asyncio.sleep(0.01)
            at_transport.feed("OK")
        asyncio.create_task(respond())

        session = await service.dial("+123****7890")
        assert session.number == "+123****7890"
        assert session.direction == "outbound"
        assert service.state == CallState.DIALING
        assert "ATD+123****7890;" in at_transport.last_written

    async def test_dial_info_log_redacts_outbound_number(self, service, at_transport, caplog):
        """Outbound dial logs must not expose the raw destination number."""
        number = "+15551230000"

        async def respond():
            await asyncio.sleep(0.01)
            at_transport.feed("OK")
        asyncio.create_task(respond())

        with caplog.at_level(logging.INFO, logger="callstack.voice.service"):
            await service.dial(number)

        assert number not in caplog.text
        assert "Dialing" in caplog.text

    async def test_inbound_call_info_logs_redact_caller_number(self, service, bus, at_transport, caplog):
        """Inbound caller logs must not expose the raw caller number."""
        number = "+15551230001"

        async def respond():
            await asyncio.sleep(0.01)
            at_transport.feed("OK")
            await asyncio.sleep(0.01)
            at_transport.feed("OK")
        asyncio.create_task(respond())

        with caplog.at_level(logging.INFO, logger="callstack.voice.service"):
            await bus.emit(RingEvent())
            await asyncio.sleep(0.01)
            await bus.emit(CallerIDEvent(number=number))
            await asyncio.sleep(0.01)
            await service.answer()

        assert number not in caplog.text
        assert "Caller ID:" in caplog.text
        assert "Answering call from" in caplog.text

    async def test_dial_failure(self, service, at_transport):
        async def respond():
            await asyncio.sleep(0.01)
            at_transport.feed("ERROR")
        asyncio.create_task(respond())

        with pytest.raises(DialError):
            await service.dial("+1234567890")

        assert service.state == CallState.IDLE

    @pytest.mark.parametrize(
        "terminal_result",
        ["BUSY", "NO CARRIER", "NO ANSWER", "NO DIALTONE", "NO DIAL TONE"],
    )
    async def test_dial_terminal_result_fails_promptly_with_dial_error(
        self, service, at_transport, terminal_result
    ):
        async def respond():
            await asyncio.sleep(0.01)
            at_transport.feed(terminal_result, "OK")
        asyncio.create_task(respond())

        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(DialError) as exc_info:
            await service.dial("+123****7890", timeout=0.5)
        elapsed = loop.time() - started

        assert terminal_result in str(exc_info.value)
        assert exc_info.value.lines == [terminal_result]
        assert elapsed < 0.25
        assert service.state == CallState.IDLE
        assert service.active_call is None

    async def test_dial_terminal_failure_cleans_up_inflight_audio_enable(self, bus):
        class LockedAT:
            def __init__(self):
                self.calls: list[str] = []
                self._lock = asyncio.Lock()

            async def execute(self, command, **kwargs):
                async with self._lock:
                    self.calls.append(command)
                    if command.startswith("ATD"):
                        await bus.emit(CallStateEvent(state=CallState.ACTIVE))
                        await asyncio.sleep(0.01)
                        return ATResponse(success=False, lines=["NO CARRIER"])
                    return ATResponse(success=True, lines=["OK"])

        class FakeAudio:
            def __init__(self):
                self.running = False

            async def start(self):
                self.running = True

            async def stop(self):
                self.running = False

        at = LockedAT()
        audio = FakeAudio()
        service = CallService(at, audio, bus)

        with pytest.raises(DialError):
            await service.dial("+123****7890")
        await asyncio.sleep(0.05)

        assert service.state == CallState.IDLE
        assert service.active_call is None
        assert audio.running is False
        assert at.calls == ["ATD+123****7890;", "AT+CPCMREG=1", "AT+CPCMREG=0"]

    async def test_incoming_ring_transitions_to_ringing(self, service, bus):
        await bus.emit(RingEvent())
        await asyncio.sleep(0.01)
        assert service.state == CallState.RINGING

    async def test_caller_id_captured(self, service, bus):
        await bus.emit(CallerIDEvent(number="+9876543210"))
        await asyncio.sleep(0.01)
        assert service._pending_caller == "+9876543210"

    async def test_answer_success(self, service, bus, at_transport):
        # Simulate incoming ring
        await bus.emit(RingEvent())
        await asyncio.sleep(0.01)
        await bus.emit(CallerIDEvent(number="+5551234"))
        await asyncio.sleep(0.01)

        # Queue answer response
        async def respond():
            await asyncio.sleep(0.01)
            at_transport.feed("OK")
            # Also queue CPCMREG response for audio enable
            await asyncio.sleep(0.01)
            at_transport.feed("OK")
        asyncio.create_task(respond())

        session = await service.answer()
        assert session.number == "+5551234"
        assert session.direction == "inbound"
        assert service.state == CallState.ACTIVE

    async def test_answer_treats_active_urc_during_ata_as_success(self, bus):
        """VOICE CALL: BEGIN during ATA should not double-transition ACTIVE."""

        class FakeAT:
            def __init__(self):
                self.calls: list[str] = []
                self._lock = asyncio.Lock()

            async def execute(self, command, **kwargs):
                async with self._lock:
                    self.calls.append(command)
                    if command == "ATA":
                        await bus.emit(CallStateEvent(state=CallState.ACTIVE))
                        await asyncio.sleep(0.01)
                    return ATResponse(success=True, lines=["OK"])

        class FakeAudio:
            def __init__(self):
                self.running = False
                self.starts = 0

            async def start(self):
                self.running = True
                self.starts += 1

            async def stop(self):
                self.running = False

        at = FakeAT()
        audio = FakeAudio()
        service = CallService(at, audio, bus)
        await bus.emit(RingEvent())
        await asyncio.sleep(0.01)
        await bus.emit(CallerIDEvent(number="+5****34"))
        await asyncio.sleep(0.01)

        session = await service.answer()
        await asyncio.sleep(0.01)

        assert session.number == "+5****34"
        assert session.direction == "inbound"
        assert service.state == CallState.ACTIVE
        assert audio.starts == 1
        assert at.calls == ["ATA", "AT+CPCMREG=1"]

    async def test_active_urc_and_answer_do_not_register_audio_twice_after_start_failure(self, bus):
        class FakeAT:
            def __init__(self):
                self.calls: list[str] = []

            async def execute(self, command, **kwargs):
                self.calls.append(command)
                if command == "ATA":
                    await bus.emit(CallStateEvent(state=CallState.ACTIVE))
                    await asyncio.sleep(0.01)
                return ATResponse(success=True, lines=["OK"])

        class FakeAudio:
            running = False

            async def start(self):
                raise RuntimeError("audio transport failed to open")

            async def stop(self):
                self.running = False

        at = FakeAT()
        audio = FakeAudio()
        service = CallService(at, audio, bus)
        await bus.emit(RingEvent())
        await asyncio.sleep(0.01)
        await bus.emit(CallerIDEvent(number="+5****34"))
        await asyncio.sleep(0.01)

        session = await service.answer()
        await asyncio.sleep(0.01)

        assert session.is_active
        assert audio.running is False
        assert at.calls == ["ATA", "AT+CPCMREG=1"]

    async def test_answer_keeps_active_call_when_audio_enable_times_out(self, bus):
        class FakeAT:
            def __init__(self):
                self.calls: list[str] = []

            async def execute(self, command, **kwargs):
                self.calls.append(command)
                if command == "AT+CPCMREG=1":
                    raise ATTimeoutError("audio enable timed out")
                return ATResponse(success=True, lines=["OK"])

        class FakeAudio:
            def __init__(self):
                self.running = False

            async def start(self):
                self.running = True

            async def stop(self):
                self.running = False

        at = FakeAT()
        audio = FakeAudio()
        service = CallService(at, audio, bus)
        await bus.emit(RingEvent())
        await asyncio.sleep(0.01)
        await bus.emit(CallerIDEvent(number="+5****34"))
        await asyncio.sleep(0.01)

        session = await service.answer()

        assert session.number == "+5****34"
        assert session.direction == "inbound"
        assert service.state == CallState.ACTIVE
        assert service.active_call is session
        assert service._pending_caller is None
        assert audio.running is False
        assert at.calls == ["ATA", "AT+CPCMREG=1"]

    async def test_answer_keeps_active_call_when_audio_start_fails(self, bus):
        class FakeAT:
            def __init__(self):
                self.calls: list[str] = []

            async def execute(self, command, **kwargs):
                self.calls.append(command)
                return ATResponse(success=True, lines=["OK"])

        class FakeAudio:
            running = False

            async def start(self):
                raise RuntimeError("audio transport failed to open")

            async def stop(self):
                self.running = False

        at = FakeAT()
        audio = FakeAudio()
        service = CallService(at, audio, bus)
        await bus.emit(RingEvent())
        await asyncio.sleep(0.01)
        await bus.emit(CallerIDEvent(number="+5****34"))
        await asyncio.sleep(0.01)

        session = await service.answer()

        assert session.number == "+5****34"
        assert session.direction == "inbound"
        assert service.state == CallState.ACTIVE
        assert service.active_call is session
        assert service._pending_caller is None
        assert audio.running is False
        assert at.calls == ["ATA", "AT+CPCMREG=1"]

        await service.hangup()
        assert service.state == CallState.IDLE
        assert service.active_call is None
        assert at.calls == ["ATA", "AT+CPCMREG=1", "ATH", "AT+CPCMREG=0"]

    async def test_answer_failure_resets_ringing_state_for_next_call(self, service, bus, at_transport):
        await bus.emit(RingEvent())
        await asyncio.sleep(0.01)
        await bus.emit(CallerIDEvent(number="+5****34"))
        await asyncio.sleep(0.01)

        async def respond():
            await asyncio.sleep(0.01)
            at_transport.feed("ERROR")
        asyncio.create_task(respond())

        with pytest.raises(AnswerError):
            await service.answer()

        assert service.state == CallState.IDLE
        assert service.active_call is None
        assert service._pending_caller is None

        await bus.emit(RingEvent())
        await asyncio.sleep(0.01)
        assert service.state == CallState.RINGING

    async def test_answer_timeout_resets_ringing_state_for_next_call(self, service, bus, monkeypatch):
        await bus.emit(RingEvent())
        await asyncio.sleep(0.01)
        await bus.emit(CallerIDEvent(number="+5****34"))
        await asyncio.sleep(0.01)

        async def raise_timeout(*args, **kwargs):
            raise ATTimeoutError("ATA timed out")
        monkeypatch.setattr(service._at, "execute", raise_timeout)

        with pytest.raises(ATTimeoutError):
            await service.answer()

        assert service.state == CallState.IDLE
        assert service.active_call is None
        assert service._pending_caller is None

        await bus.emit(RingEvent())
        await asyncio.sleep(0.01)
        assert service.state == CallState.RINGING

    async def test_answer_timeout_after_active_urc_cleans_audio_and_next_ring(self, bus):
        class FakeAT:
            def __init__(self):
                self.calls: list[str] = []

            async def execute(self, command, **kwargs):
                self.calls.append(command)
                if command == "ATA":
                    await bus.emit(CallStateEvent(state=CallState.ACTIVE))
                    await asyncio.sleep(0.01)
                    raise ATTimeoutError("ATA timed out after active URC")
                return ATResponse(success=True, lines=["OK"])

        class FakeAudio:
            def __init__(self):
                self.running = False

            async def start(self):
                self.running = True

            async def stop(self):
                self.running = False

        at = FakeAT()
        audio = FakeAudio()
        service = CallService(at, audio, bus)
        await bus.emit(RingEvent())
        await asyncio.sleep(0.01)
        await bus.emit(CallerIDEvent(number="+5****34"))
        await asyncio.sleep(0.01)

        with pytest.raises(ATTimeoutError):
            await service.answer()

        assert service.state == CallState.IDLE
        assert service.active_call is None
        assert service._pending_caller is None
        assert audio.running is False
        assert at.calls == ["ATA", "AT+CPCMREG=1", "AT+CPCMREG=0"]

        await bus.emit(RingEvent())
        await asyncio.sleep(0.01)
        assert service.state == CallState.RINGING

    async def test_answer_timeout_waits_for_inflight_active_urc_audio_enable_before_cleanup(self, bus):
        class LockedAT:
            def __init__(self):
                self.calls: list[str] = []
                self._lock = asyncio.Lock()

            async def execute(self, command, **kwargs):
                async with self._lock:
                    self.calls.append(command)
                    if command == "ATA":
                        await bus.emit(CallStateEvent(state=CallState.ACTIVE))
                        await asyncio.sleep(0.01)
                        raise ATTimeoutError("ATA timed out while audio enable was queued")
                    return ATResponse(success=True, lines=["OK"])

        class FakeAudio:
            def __init__(self):
                self.running = False

            async def start(self):
                self.running = True

            async def stop(self):
                self.running = False

        at = LockedAT()
        audio = FakeAudio()
        service = CallService(at, audio, bus)
        await bus.emit(RingEvent())
        await asyncio.sleep(0.01)
        await bus.emit(CallerIDEvent(number="+5****34"))
        await asyncio.sleep(0.01)

        with pytest.raises(ATTimeoutError):
            await service.answer()
        await asyncio.sleep(0.05)

        assert service.state == CallState.IDLE
        assert service.active_call is None
        assert service._pending_caller is None
        assert audio.running is False
        assert at.calls == ["ATA", "AT+CPCMREG=1", "AT+CPCMREG=0"]

        await bus.emit(RingEvent())
        await asyncio.sleep(0.01)
        assert service.state == CallState.RINGING

    async def test_hangup(self, service, bus, at_transport):
        # Set up an active call
        await bus.emit(RingEvent())
        await asyncio.sleep(0.01)

        async def answer_respond():
            await asyncio.sleep(0.01)
            at_transport.feed("OK")
            await asyncio.sleep(0.01)
            at_transport.feed("OK")  # CPCMREG
        asyncio.create_task(answer_respond())
        await service.answer()

        # Now hangup
        async def hangup_respond():
            await asyncio.sleep(0.01)
            at_transport.feed("OK")
            await asyncio.sleep(0.01)
            at_transport.feed("OK")  # CPCMREG off
        asyncio.create_task(hangup_respond())
        await service.hangup()

        assert service.state == CallState.IDLE
        assert service.active_call is None

    async def test_service_hangup_signals_active_session_waiters(self, service, bus, at_transport):
        await bus.emit(RingEvent())
        await asyncio.sleep(0.01)

        async def answer_respond():
            await asyncio.sleep(0.01)
            at_transport.feed("OK")
            await asyncio.sleep(0.01)
            at_transport.feed("OK")  # CPCMREG on
        asyncio.create_task(answer_respond())
        session = await service.answer()

        async def hangup_respond():
            await asyncio.sleep(0.01)
            at_transport.feed("OK")
            await asyncio.sleep(0.01)
            at_transport.feed("OK")  # CPCMREG off
        asyncio.create_task(hangup_respond())

        await service.hangup()

        assert service.state == CallState.IDLE
        assert service.active_call is None
        assert await session.wait_for_end(timeout=0.01) is True

    async def test_failed_service_hangup_response_keeps_session_active(self, bus):
        class FailingHangupAT:
            async def execute(self, command, **kwargs):
                if command == "ATH":
                    return ATResponse(success=False, lines=["ERROR"])
                return ATResponse(success=True, lines=["OK"])

        class FakeAudio:
            running = False

            async def start(self):
                self.running = True

            async def stop(self):
                self.running = False

        service = CallService(
            cast(ATCommandExecutor, FailingHangupAT()),
            cast(AudioPipeline, FakeAudio()),
            bus,
        )
        await service._fsm.transition(CallState.DIALING)
        await service._fsm.transition(CallState.ACTIVE)
        session = CallSession(number="5551234", direction="outbound", service=service)
        service._active_call = session

        with pytest.raises(ATCommandError):
            await service.hangup()

        assert service.state == CallState.ACTIVE
        assert service.active_call is session
        assert await session.wait_for_end(timeout=0.01) is False

    async def test_remote_hangup_via_urc(self, service, bus, at_transport):
        """When remote party hangs up, VOICE CALL: END URC triggers cleanup."""
        # Set up an active call
        await bus.emit(RingEvent())
        await asyncio.sleep(0.01)

        async def answer_respond():
            await asyncio.sleep(0.01)
            at_transport.feed("OK")
            await asyncio.sleep(0.01)
            at_transport.feed("OK")  # CPCMREG
        asyncio.create_task(answer_respond())
        await service.answer()
        assert service.state == CallState.ACTIVE

        # Simulate remote hangup — CPCMREG off response
        async def cpcm_respond():
            await asyncio.sleep(0.02)
            at_transport.feed("OK")
        asyncio.create_task(cpcm_respond())

        await bus.emit(CallStateEvent(state=CallState.ENDED))
        await asyncio.sleep(0.05)

        assert service.state == CallState.IDLE

    async def test_idle_no_carrier_urc_hangs_up_active_call(
        self, service, executor, bus, at_transport
    ):
        """NO CARRIER remains an idle URC that cleans up an active call."""
        await executor.start_reader()
        try:
            await bus.emit(RingEvent())
            await asyncio.sleep(0.01)

            async def answer_respond():
                await asyncio.sleep(0.01)
                at_transport.feed("OK")
                await asyncio.sleep(0.01)
                at_transport.feed("OK")  # CPCMREG on
            asyncio.create_task(answer_respond())
            session = await service.answer()
            assert session.is_active

            async def cpcm_off_respond():
                await asyncio.sleep(0.02)
                at_transport.feed("OK")
            asyncio.create_task(cpcm_off_respond())

            at_transport.feed("NO CARRIER")
            await asyncio.sleep(0.05)

            assert service.state == CallState.IDLE
            assert service.active_call is None
            assert session.is_active is False
        finally:
            await executor.stop_reader()

    async def test_remote_hangup_waits_for_inflight_audio_enable_before_cleanup(self, bus):
        class SlowAT:
            def __init__(self):
                self.calls: list[str] = []
                self._lock = asyncio.Lock()
                self.cpcmreg_on_started = asyncio.Event()
                self.finish_cpcmreg_on = asyncio.Event()

            async def execute(self, command, **kwargs):
                async with self._lock:
                    self.calls.append(command)
                    if command == "AT+CPCMREG=1":
                        self.cpcmreg_on_started.set()
                        await self.finish_cpcmreg_on.wait()
                    return ATResponse(success=True, lines=["OK"])

        class FakeAudio:
            def __init__(self):
                self.running = False

            async def start(self):
                self.running = True

            async def stop(self):
                self.running = False

        at = SlowAT()
        audio = FakeAudio()
        service = CallService(at, audio, bus)
        await bus.emit(RingEvent())
        await asyncio.sleep(0.01)
        await bus.emit(CallStateEvent(state=CallState.ACTIVE))
        await asyncio.wait_for(at.cpcmreg_on_started.wait(), timeout=1.0)

        await bus.emit(CallStateEvent(state=CallState.ENDED))
        await asyncio.sleep(0.01)
        at.finish_cpcmreg_on.set()
        await asyncio.sleep(0.05)

        assert service.state == CallState.IDLE
        assert service.active_call is None
        assert audio.running is False
        assert at.calls == ["AT+CPCMREG=1", "AT+CPCMREG=0"]

    async def test_local_hangup_waits_for_inflight_audio_enable_before_cleanup(self, bus):
        class SlowAT:
            def __init__(self):
                self.calls: list[str] = []
                self.cpcmreg_on_started = asyncio.Event()
                self.finish_cpcmreg_on = asyncio.Event()

            async def execute(self, command, **kwargs):
                self.calls.append(command)
                if command == "AT+CPCMREG=1":
                    self.cpcmreg_on_started.set()
                    await self.finish_cpcmreg_on.wait()
                return ATResponse(success=True, lines=["OK"])

        class FakeAudio:
            def __init__(self):
                self.running = False

            async def start(self):
                self.running = True

            async def stop(self):
                self.running = False

        at = SlowAT()
        audio = FakeAudio()
        service = CallService(at, audio, bus)
        await bus.emit(RingEvent())
        await asyncio.sleep(0.01)
        await bus.emit(CallStateEvent(state=CallState.ACTIVE))
        await asyncio.wait_for(at.cpcmreg_on_started.wait(), timeout=1.0)

        hangup_task = asyncio.create_task(service.hangup())
        await asyncio.sleep(0.01)
        at.finish_cpcmreg_on.set()
        await hangup_task
        await asyncio.sleep(0.01)

        assert service.state == CallState.IDLE
        assert service.active_call is None
        assert audio.running is False
        assert at.calls == ["AT+CPCMREG=1", "ATH", "AT+CPCMREG=0"]

    async def test_active_urc_rechecks_state_before_audio_enable_after_remote_hangup(self, bus):
        class FakeAT:
            def __init__(self):
                self.calls: list[str] = []

            async def execute(self, command, **kwargs):
                self.calls.append(command)
                return ATResponse(success=True, lines=["OK"])

        class FakeAudio:
            def __init__(self):
                self.running = False

            async def start(self):
                self.running = True

            async def stop(self):
                self.running = False

        at = FakeAT()
        audio = FakeAudio()
        service = CallService(at, audio, bus)

        async def end_call_during_active_transition(old_state, new_state):
            if new_state == CallState.ACTIVE:
                await bus.emit(CallStateEvent(state=CallState.ENDED))
                await asyncio.sleep(0.01)
        service._fsm.on_transition(end_call_during_active_transition)

        await bus.emit(RingEvent())
        await asyncio.sleep(0.01)
        await bus.emit(CallStateEvent(state=CallState.ACTIVE))
        await asyncio.sleep(0.05)

        assert service.state == CallState.IDLE
        assert audio.running is False
        assert at.calls == []

    async def test_reject_incoming_call(self, service, bus, at_transport):
        await bus.emit(RingEvent())
        await asyncio.sleep(0.01)
        assert service.state == CallState.RINGING

        async def respond():
            await asyncio.sleep(0.01)
            at_transport.feed("OK")
        asyncio.create_task(respond())

        await service.reject()
        assert service.state == CallState.IDLE


class TestCallSession:

    async def test_session_is_active(self, service, bus, at_transport):
        await bus.emit(RingEvent())
        await asyncio.sleep(0.01)

        async def respond():
            await asyncio.sleep(0.01)
            at_transport.feed("OK")
            await asyncio.sleep(0.01)
            at_transport.feed("OK")
        asyncio.create_task(respond())

        session = await service.answer()
        assert session.is_active

    async def test_session_play(self, service, bus, at_transport, audio_transport, tmp_path):
        await bus.emit(RingEvent())
        await asyncio.sleep(0.01)

        async def respond():
            await asyncio.sleep(0.01)
            at_transport.feed("OK")
            await asyncio.sleep(0.01)
            at_transport.feed("OK")
        asyncio.create_task(respond())

        session = await service.answer()

        wav_path = _make_wav(str(tmp_path / "test.wav"))
        await session.play(wav_path)
        assert len(audio_transport._written) > 0

    async def test_session_record(self, service, bus, at_transport, audio_transport, tmp_path):
        await bus.emit(RingEvent())
        await asyncio.sleep(0.01)

        async def respond():
            await asyncio.sleep(0.01)
            at_transport.feed("OK")
            await asyncio.sleep(0.01)
            at_transport.feed("OK")
        asyncio.create_task(respond())

        session = await service.answer()

        # Feed some audio data
        audio_transport.feed_raw(b"\x00\x80" * 320)

        output = str(tmp_path / "rec.wav")
        result = await session.record(output, max_duration=0.1)
        assert result == output

    async def test_record_rejects_stale_session_before_audio_read(self, tmp_path):
        class FakeAudio:
            def __init__(self):
                self.record_calls = []

            async def record(self, output_path, max_duration=60.0, stop_on_dtmf=False):
                self.record_calls.append((output_path, max_duration, stop_on_dtmf))
                return output_path

        class FakeService:
            def __init__(self):
                self.state = CallState.ACTIVE
                self.active_call: object | None = None
                self._audio = FakeAudio()

        service = FakeService()
        stale_session = CallSession(
            number="5551234", direction="inbound", service=cast(CallService, service)
        )
        current_session = CallSession(
            number="5555678", direction="inbound", service=cast(CallService, service)
        )
        service.active_call = current_session

        with pytest.raises(RuntimeError, match="active call"):
            await stale_session.record(str(tmp_path / "stale-recording.wav"), max_duration=0.1)

        assert service._audio.record_calls == []

    async def test_hangup_rejects_stale_session_before_service_hangup(self):
        class FakeService:
            def __init__(self):
                self.state = CallState.ACTIVE
                self.active_call: object | None = None
                self.hangup_calls = 0

            async def hangup(self):
                self.hangup_calls += 1

        service = FakeService()
        stale_session = CallSession(
            number="5551234", direction="inbound", service=cast(CallService, service)
        )
        current_session = CallSession(
            number="5555678", direction="inbound", service=cast(CallService, service)
        )
        service.active_call = current_session

        with pytest.raises(RuntimeError, match="active call"):
            await stale_session.hangup()

        assert service.hangup_calls == 0
        assert not stale_session._ended.is_set()

    async def test_hangup_rejects_inactive_session_before_service_hangup(self):
        class FakeService:
            def __init__(self):
                self.state = CallState.IDLE
                self.active_call = None
                self.hangup_calls = 0

            async def hangup(self):
                self.hangup_calls += 1

        service = FakeService()
        session = CallSession(
            number="5551234", direction="inbound", service=cast(CallService, service)
        )

        with pytest.raises(RuntimeError, match="active call"):
            await session.hangup()

        assert service.hangup_calls == 0
        assert not session._ended.is_set()

    async def test_hangup_allows_current_dialing_session_to_cancel_call(self):
        class FakeService:
            def __init__(self):
                self.state = CallState.DIALING
                self.active_call: object | None = None
                self.hangup_calls = 0

            async def hangup(self):
                self.hangup_calls += 1
                self.state = CallState.IDLE
                self.active_call = None

        service = FakeService()
        session = CallSession(
            number="5551234", direction="outbound", service=cast(CallService, service)
        )
        service.active_call = session

        await session.hangup()

        assert service.hangup_calls == 1
        assert session._ended.is_set()

    async def test_collect_dtmf_rejects_stale_session_before_subscribing(self):
        class FakeService:
            def __init__(self):
                self.state = CallState.ACTIVE
                self.active_call: object | None = None
                self._bus = EventBus()

        service = FakeService()
        stale_session = CallSession(
            number="5551234", direction="inbound", service=cast(CallService, service)
        )
        current_session = CallSession(
            number="5555678", direction="inbound", service=cast(CallService, service)
        )
        service.active_call = current_session

        with pytest.raises(RuntimeError, match="active call"):
            await stale_session.collect_dtmf(max_digits=4, timeout=0.01)

        assert service._bus._queues[DTMFEvent] == []

    async def test_collect_dtmf_aborts_if_session_becomes_stale_before_digit(self):
        class FakeService:
            def __init__(self):
                self.state = CallState.ACTIVE
                self.active_call: object | None = None
                self._bus = EventBus()

        service = FakeService()
        session = CallSession(
            number="5551234", direction="inbound", service=cast(CallService, service)
        )
        service.active_call = session
        collect_task = asyncio.create_task(session.collect_dtmf(max_digits=1, timeout=0.5))
        await _wait_for_dtmf_stream(service._bus)

        service.active_call = CallSession(
            number="5555678", direction="inbound", service=cast(CallService, service)
        )
        await service._bus.emit(DTMFEvent(digit="8"))

        with pytest.raises(RuntimeError, match="active call"):
            await collect_task

    async def test_play_and_collect_aborts_if_session_becomes_stale_before_digit(self):
        class FakeAudio:
            def __init__(self):
                self.started = asyncio.Event()
                self.cancelled = False

            async def play_file(self, path, cancel=None):
                self.started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise

        class FakeService:
            def __init__(self):
                self.state = CallState.ACTIVE
                self.active_call: object | None = None
                self._bus = EventBus()
                self._audio = FakeAudio()

        service = FakeService()
        session = CallSession(
            number="5551234", direction="inbound", service=cast(CallService, service)
        )
        service.active_call = session
        collect_task = asyncio.create_task(
            session.play_and_collect("menu.wav", max_digits=1, timeout=0.5, interrupt=True)
        )
        await service._audio.started.wait()
        await _wait_for_dtmf_stream(service._bus)

        service.active_call = CallSession(
            number="5555678", direction="inbound", service=cast(CallService, service)
        )
        await service._bus.emit(DTMFEvent(digit="7"))

        with pytest.raises(RuntimeError, match="active call"):
            await collect_task
        assert service._audio.cancelled is True

    async def test_send_dtmf_rejects_inactive_session_before_modem_write(self):
        class FakeAT:
            def __init__(self):
                self.commands = []

            async def execute(self, command, **kwargs):
                self.commands.append((command, kwargs))
                return ATResponse(success=True, lines=["OK"])

        class FakeService:
            state = CallState.IDLE
            active_call = None

            def __init__(self):
                self._at = FakeAT()

        service = FakeService()
        session = CallSession(number="+1234", direction="outbound", service=service)

        with pytest.raises(RuntimeError, match="active call"):
            await session.send_dtmf("5")

        assert service._at.commands == []

    async def test_send_dtmf_rejects_inactive_empty_digits_before_modem_write(self):
        class FakeAT:
            def __init__(self):
                self.commands = []

            async def execute(self, command, **kwargs):
                self.commands.append((command, kwargs))
                return ATResponse(success=True, lines=["OK"])

        class FakeService:
            state = CallState.IDLE
            active_call = None

            def __init__(self):
                self._at = FakeAT()

        service = FakeService()
        session = CallSession(number="+1234", direction="outbound", service=service)

        with pytest.raises(RuntimeError, match="active call"):
            await session.send_dtmf("")

        assert service._at.commands == []

    async def test_send_dtmf_rejects_stale_session_when_another_call_is_active(self):
        class FakeAT:
            def __init__(self):
                self.commands = []

            async def execute(self, command, **kwargs):
                self.commands.append((command, kwargs))
                return ATResponse(success=True, lines=["OK"])

        class FakeService:
            def __init__(self):
                self.state = CallState.ACTIVE
                self.active_call = None
                self._at = FakeAT()

        service = FakeService()
        stale_session = CallSession(number="+1234", direction="outbound", service=service)
        current_session = CallSession(number="+5678", direction="outbound", service=service)
        service.active_call = current_session

        with pytest.raises(RuntimeError, match="active call"):
            await stale_session.send_dtmf("5")

        assert service._at.commands == []

    async def test_send_dtmf_stops_if_call_becomes_inactive_between_digits(self):
        class FakeService:
            def __init__(self):
                self.state = CallState.ACTIVE
                self.active_call = None
                self._at = FakeAT(self)

        class FakeAT:
            def __init__(self, service):
                self.service = service
                self.commands = []

            async def execute(self, command, **kwargs):
                self.commands.append((command, kwargs))
                self.service.state = CallState.IDLE
                return ATResponse(success=True, lines=["OK"])

        service = FakeService()
        session = CallSession(number="+1234", direction="outbound", service=service)
        service.active_call = session

        with pytest.raises(RuntimeError, match="active call"):
            await session.send_dtmf("56", duration_ms=300, inter_digit_delay_ms=0)

        assert [command for command, _kwargs in service._at.commands] == ["AT+VTS=5,3"]

    async def test_send_dtmf_uses_separate_inter_digit_delay(self, monkeypatch):
        class FakeAT:
            def __init__(self):
                self.commands = []

            async def execute(self, command, **kwargs):
                self.commands.append((command, kwargs))
                return ATResponse(success=True, lines=["OK"])

        class FakeService:
            def __init__(self):
                self.state = CallState.ACTIVE
                self.active_call = None
                self._at = FakeAT()
                self._command_timeout = 2.0

        sleeps = []

        async def fake_sleep(delay):
            sleeps.append(delay)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        service = FakeService()
        session = CallSession(number="+1234", direction="outbound", service=service)
        service.active_call = session

        await session.send_dtmf("56", duration_ms=300, inter_digit_delay_ms=50)

        assert [command for command, _kwargs in service._at.commands] == [
            "AT+VTS=5,3",
            "AT+VTS=6,3",
        ]
        assert sleeps == [0.05]

    @pytest.mark.parametrize("digits", ["X", "12X"])
    async def test_send_dtmf_rejects_invalid_sequence_before_modem_write(self, digits):
        class FakeAT:
            def __init__(self):
                self.commands = []

            async def execute(self, command, **kwargs):
                self.commands.append((command, kwargs))
                return ATResponse(success=True, lines=["OK"])

        class FakeService:
            def __init__(self):
                self.state = CallState.ACTIVE
                self.active_call: object | None = None
                self._at = FakeAT()

        service = FakeService()
        session = CallSession(number="+1234", direction="outbound", service=cast(CallService, service))
        service.active_call = session

        with pytest.raises(ValueError, match="Invalid DTMF digit"):
            await session.send_dtmf(digits)

        assert service._at.commands == []

    async def test_send_dtmf_rejects_invalid_duration_before_modem_write(self):
        class FakeAT:
            def __init__(self):
                self.commands = []

            async def execute(self, command, **kwargs):
                self.commands.append((command, kwargs))
                return ATResponse(success=True, lines=["OK"])

        class FakeService:
            def __init__(self):
                self.state = CallState.ACTIVE
                self.active_call = None
                self._at = FakeAT()

        service = FakeService()
        session = CallSession(number="+1234", direction="outbound", service=service)
        service.active_call = session

        with pytest.raises(ValueError, match="DTMF duration"):
            await session.send_dtmf("5", duration_ms=250)

        assert service._at.commands == []

    async def test_wait_for_end(self, service):
        session = CallSession(number="+1234", direction="outbound", service=service)
        # Should timeout since nobody sets _ended
        result = await session.wait_for_end(timeout=0.05)
        assert result is False

        # Now set it
        session._ended.set()
        result = await session.wait_for_end(timeout=0.05)
        assert result is True
