"""Tests for CallService and CallSession."""

import asyncio
import wave
import pytest

from callstack.transport.mock import MockTransport
from callstack.events.bus import EventBus
from callstack.events.types import (
    CallState,
    CallStateEvent,
    CallerIDEvent,
    RingEvent,
)
from callstack.errors import DialError, AnswerError
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

    async def test_dial_success(self, service, at_transport):
        # Queue the modem response
        async def respond():
            await asyncio.sleep(0.01)
            at_transport.feed("OK")
        asyncio.create_task(respond())

        session = await service.dial("+1234567890")
        assert session.number == "+1234567890"
        assert session.direction == "outbound"
        assert service.state == CallState.DIALING
        assert "ATD+1234567890;" in at_transport.last_written

    async def test_dial_failure(self, service, at_transport):
        async def respond():
            await asyncio.sleep(0.01)
            at_transport.feed("ERROR")
        asyncio.create_task(respond())

        with pytest.raises(DialError):
            await service.dial("+1234567890")

        assert service.state == CallState.IDLE

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
        await bus.emit(CallerIDEvent(number="+5551234"))
        await asyncio.sleep(0.01)

        session = await service.answer()
        await asyncio.sleep(0.01)

        assert session.number == "+5551234"
        assert session.direction == "inbound"
        assert service.state == CallState.ACTIVE
        assert audio.starts == 1
        assert at.calls == ["ATA", "AT+CPCMREG=1"]

    async def test_answer_failure(self, service, bus, at_transport):
        await bus.emit(RingEvent())
        await asyncio.sleep(0.01)

        async def respond():
            await asyncio.sleep(0.01)
            at_transport.feed("ERROR")
        asyncio.create_task(respond())

        with pytest.raises(AnswerError):
            await service.answer()

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
            await session.send_dtmf("56", duration_ms=0)

        assert [command for command, _kwargs in service._at.commands] == ["AT+VTS=5"]

    async def test_wait_for_end(self, service):
        session = CallSession(number="+1234", direction="outbound", service=service)
        # Should timeout since nobody sets _ended
        result = await session.wait_for_end(timeout=0.05)
        assert result is False

        # Now set it
        session._ended.set()
        result = await session.wait_for_end(timeout=0.05)
        assert result is True
