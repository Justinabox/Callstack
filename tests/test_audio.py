"""Tests for AudioPlayer and AudioPipeline."""

import asyncio
import math
import struct
import tempfile
import wave
import os
from typing import cast

import pytest
from callstack.transport.base import Transport
from callstack.transport.mock import MockTransport
from callstack.events.bus import EventBus
from callstack.events.types import DTMFEvent, CallState
from callstack.voice.player import AudioPlayer
from callstack.voice.audio import AudioPipeline
from callstack.voice.service import CallService, CallSession
from callstack.errors import AudioFormatError, AudioPipelineError


def _make_wav(path: str, rate: int = 8000, channels: int = 1,
              sampwidth: int = 2, num_frames: int = 800) -> str:
    """Create a valid test WAV file."""
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(rate)
        # Write silence (zeros)
        wf.writeframes(b"\x00" * num_frames * channels * sampwidth)
    return path


@pytest.fixture
def transport():
    t = MockTransport()
    t._open = True
    return t


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def player(transport):
    return AudioPlayer(transport)


@pytest.fixture
def pipeline(transport, bus):
    return AudioPipeline(transport, bus)


@pytest.fixture
def valid_wav(tmp_path):
    return _make_wav(str(tmp_path / "test.wav"))


class SilentTimeoutTransport:
    """Audio transport that stays open but never yields PCM before deadlines."""

    def __init__(self):
        self.reads = 0
        self._open = False

    async def open(self):
        self._open = True

    async def close(self):
        self._open = False

    async def write(self, data):
        pass

    async def read(self, size=-1) -> bytes:
        self.reads += 1
        await asyncio.sleep(10)
        return b"unreachable"

    async def readline(self):
        return b""

    def in_waiting(self):
        return 0


class ChunkThenTimeoutTransport(SilentTimeoutTransport):
    """Audio transport that returns preset chunks before going silent."""

    def __init__(self, chunks: list[bytes]):
        super().__init__()
        self._chunks = list(chunks)

    async def read(self, size=-1) -> bytes:
        self.reads += 1
        if self._chunks:
            return self._chunks.pop(0)
        await asyncio.sleep(10)
        return b"unreachable"


@pytest.fixture
def short_wav(tmp_path):
    """Very short WAV for fast playback tests."""
    return _make_wav(str(tmp_path / "short.wav"), num_frames=320)


class TestAudioPlayer:

    async def test_validate_valid_file(self, player, valid_wav):
        player.validate(valid_wav)  # Should not raise

    async def test_validate_wrong_sample_rate(self, player, tmp_path):
        path = _make_wav(str(tmp_path / "bad_rate.wav"), rate=44100)
        with pytest.raises(AudioFormatError, match="sample rate"):
            player.validate(path)

    async def test_validate_wrong_channels(self, player, tmp_path):
        path = _make_wav(str(tmp_path / "stereo.wav"), channels=2)
        with pytest.raises(AudioFormatError, match="channels"):
            player.validate(path)

    async def test_validate_wrong_sample_width(self, player, tmp_path):
        path = _make_wav(str(tmp_path / "8bit.wav"), sampwidth=1)
        with pytest.raises(AudioFormatError, match="sample width"):
            player.validate(path)

    async def test_play_writes_data(self, player, transport, short_wav):
        await player.play(short_wav)
        assert len(transport._written) > 0
        total_bytes = sum(len(d) for d in transport._written)
        assert total_bytes == 320 * 2  # 320 frames * 2 bytes

    async def test_play_cancel(self, player, transport, valid_wav):
        cancel = asyncio.Event()
        cancel.set()  # Cancel immediately
        await player.play(valid_wav, cancel=cancel)
        assert len(transport._written) == 0

    async def test_play_sequence(self, player, transport, tmp_path):
        wav1 = _make_wav(str(tmp_path / "a.wav"), num_frames=320)
        wav2 = _make_wav(str(tmp_path / "b.wav"), num_frames=320)
        await player.play_sequence([wav1, wav2])
        total_bytes = sum(len(d) for d in transport._written)
        assert total_bytes == 2 * 320 * 2

    async def test_play_sequence_cancel(self, player, transport, tmp_path):
        wav1 = _make_wav(str(tmp_path / "a.wav"), num_frames=320)
        wav2 = _make_wav(str(tmp_path / "b.wav"), num_frames=320)
        cancel = asyncio.Event()
        cancel.set()
        await player.play_sequence([wav1, wav2], cancel=cancel)
        assert len(transport._written) == 0

    async def test_play_loop_respects_cancel(self, player, transport, short_wav):
        cancel = asyncio.Event()
        loop_count = 0
        original_play = player.play

        async def counting_play(path, c=None):
            nonlocal loop_count
            loop_count += 1
            if loop_count >= 3:
                cancel.set()
            await original_play(path, c)

        player.play = counting_play
        await player.play_loop(short_wav, cancel=cancel)
        assert loop_count >= 3


class TestAudioPipeline:

    async def test_start_stop(self, pipeline, transport):
        transport._open = False
        await pipeline.start()
        assert pipeline.running
        assert transport._open

        await pipeline.stop()
        assert not pipeline.running
        assert not transport._open

    async def test_play_file(self, pipeline, transport, short_wav):
        pipeline._running = True
        await pipeline.play_file(short_wav)
        assert len(transport._written) > 0

    async def test_record_basic(self, pipeline, transport, tmp_path):
        output = str(tmp_path / "recording.wav")
        pipeline._running = True

        # Feed some audio data to the transport
        audio_data = b"\x00\x80" * 320  # 320 frames of audio
        transport.feed_raw(audio_data)

        recorded = await pipeline.record(output, max_duration=0.1)

        assert recorded == output
        assert os.path.exists(output)

        # Verify the recorded WAV has correct format
        with wave.open(output, "rb") as wf:
            assert wf.getframerate() == 8000
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2

    async def test_record_fails_closed_when_pipeline_is_not_running(
        self, pipeline, tmp_path
    ):
        output = tmp_path / "inactive-recording.wav"

        with pytest.raises(AudioPipelineError, match="Audio pipeline is not running"):
            await pipeline.record(str(output), max_duration=0.1)

        assert not output.exists()

    async def test_session_record_propagates_inactive_audio_failure(
        self, pipeline, tmp_path
    ):
        service = cast(CallService, type(
            "Service",
            (),
            {"_audio": pipeline, "state": CallState.ACTIVE, "active_call": None},
        )())
        session = CallSession(number="5551234", direction="inbound", service=service)
        setattr(service, "active_call", session)

        with pytest.raises(AudioPipelineError, match="Audio pipeline is not running"):
            await session.record(str(tmp_path / "session-recording.wav"), max_duration=0.1)

    @pytest.mark.parametrize(
        "max_duration",
        [0, -0.1, math.inf, -math.inf, math.nan, True, False, "1", None, object()],
    )
    async def test_record_rejects_invalid_duration_before_writing(
        self, pipeline, tmp_path, max_duration
    ):
        output = tmp_path / "invalid-duration.wav"
        pipeline._running = True

        with pytest.raises(AudioPipelineError, match="max_duration must be positive and finite"):
            await asyncio.wait_for(
                pipeline.record(str(output), max_duration=max_duration),
                timeout=1.0,
            )

        assert not output.exists()

    async def test_session_record_propagates_boolean_duration_failure(
        self, pipeline, tmp_path
    ):
        pipeline._running = True
        service = cast(CallService, type(
            "Service",
            (),
            {"_audio": pipeline, "state": CallState.ACTIVE, "active_call": None},
        )())
        session = CallSession(number="5551234", direction="inbound", service=service)
        setattr(service, "active_call", session)

        output = tmp_path / "session-bool-duration.wav"
        with pytest.raises(AudioPipelineError, match="max_duration must be positive and finite"):
            await session.record(str(output), max_duration=True)

        assert not output.exists()

    async def test_session_record_propagates_invalid_duration_failure(
        self, pipeline, tmp_path
    ):
        pipeline._running = True
        service = cast(CallService, type(
            "Service",
            (),
            {"_audio": pipeline, "state": CallState.ACTIVE, "active_call": None},
        )())
        session = CallSession(number="5551234", direction="inbound", service=service)
        setattr(service, "active_call", session)

        with pytest.raises(AudioPipelineError, match="max_duration must be positive and finite"):
            await session.record(str(tmp_path / "session-invalid-duration.wav"), max_duration=0)

    async def test_session_record_propagates_nonnumeric_duration_failure(
        self, pipeline, tmp_path
    ):
        pipeline._running = True
        service = cast(CallService, type(
            "Service",
            (),
            {"_audio": pipeline, "state": CallState.ACTIVE, "active_call": None},
        )())
        session = CallSession(number="5551234", direction="inbound", service=service)
        setattr(service, "active_call", session)

        output = tmp_path / "session-nonnumeric-duration.wav"
        with pytest.raises(AudioPipelineError, match="max_duration must be positive and finite"):
            await session.record(str(output), max_duration="1")

        assert not output.exists()

    async def test_record_stops_on_dtmf(self, pipeline, transport, bus, tmp_path):
        output = str(tmp_path / "recording.wav")
        pipeline._running = True

        # Feed enough data to keep recording going
        for _ in range(20):
            transport.feed_raw(b"\x00\x80" * 320)

        async def emit_dtmf():
            await asyncio.sleep(0.05)
            await bus.emit(DTMFEvent(digit="1"))

        asyncio.create_task(emit_dtmf())
        recorded = await pipeline.record(output, max_duration=5.0, stop_on_dtmf=True)
        assert recorded == output

    async def test_record_respects_max_duration(self, pipeline, transport, tmp_path):
        output = str(tmp_path / "recording.wav")
        pipeline._running = True

        # Feed data continuously
        for _ in range(50):
            transport.feed_raw(b"\x00\x80" * 320)

        recorded = await pipeline.record(output, max_duration=0.05)
        assert recorded == output

    async def test_record_fails_closed_when_transport_returns_eof(
        self, pipeline, transport, tmp_path
    ):
        output = tmp_path / "eof-recording.wav"
        pipeline._running = True
        transport.feed_raw(b"")

        with pytest.raises(AudioPipelineError, match="Audio transport ended during recording"):
            await pipeline.record(str(output), max_duration=0.1)

        assert not output.exists()

    async def test_record_fails_closed_when_transport_produces_no_frames(
        self, tmp_path
    ):
        transport = SilentTimeoutTransport()
        pipeline = AudioPipeline(cast(Transport, transport), EventBus())
        output = tmp_path / "silent-recording.wav"
        await pipeline.start()

        with pytest.raises(AudioPipelineError, match="Audio transport produced no audio frames"):
            await pipeline.record(str(output), max_duration=0.03)

        assert transport.reads > 0
        assert not output.exists()
        assert pipeline.running

        await pipeline.stop()

    async def test_record_no_frames_preserves_existing_output_file(
        self, tmp_path
    ):
        transport = SilentTimeoutTransport()
        pipeline = AudioPipeline(cast(Transport, transport), EventBus())
        output = tmp_path / "existing-silent-recording.wav"
        original = b"existing recording should survive silent timeout"
        output.write_bytes(original)
        await pipeline.start()

        with pytest.raises(AudioPipelineError, match="Audio transport produced no audio frames"):
            await pipeline.record(str(output), max_duration=0.03)

        assert output.read_bytes() == original

        await pipeline.stop()

    async def test_record_counts_pcm_frames_split_across_transport_reads(
        self, tmp_path
    ):
        transport = ChunkThenTimeoutTransport([b"\x00", b"\x80"])
        pipeline = AudioPipeline(cast(Transport, transport), EventBus())
        output = tmp_path / "split-frame-recording.wav"
        await pipeline.start()

        recorded = await pipeline.record(str(output), max_duration=0.03)

        assert recorded == str(output)
        with wave.open(str(output), "rb") as wf:
            assert wf.getnframes() == 1

        await pipeline.stop()

    async def test_record_fails_closed_on_trailing_partial_frame(
        self, tmp_path
    ):
        transport = ChunkThenTimeoutTransport([b"abc"])
        pipeline = AudioPipeline(cast(Transport, transport), EventBus())
        output = tmp_path / "trailing-partial-frame.wav"
        await pipeline.start()

        with pytest.raises(AudioPipelineError, match="incomplete PCM frame"):
            await pipeline.record(str(output), max_duration=0.03)

        assert not output.exists()

        await pipeline.stop()

    async def test_record_trailing_partial_frame_preserves_existing_output_file(
        self, tmp_path
    ):
        transport = ChunkThenTimeoutTransport([b"\x00\x80", b"\x01"])
        pipeline = AudioPipeline(cast(Transport, transport), EventBus())
        output = tmp_path / "existing-trailing-partial-frame.wav"
        original = b"existing recording should survive trailing partial frame"
        output.write_bytes(original)
        await pipeline.start()

        with pytest.raises(AudioPipelineError, match="incomplete PCM frame"):
            await pipeline.record(str(output), max_duration=0.03)

        assert output.read_bytes() == original

        await pipeline.stop()

    async def test_session_record_propagates_no_audio_frames_failure(
        self, tmp_path
    ):
        transport = SilentTimeoutTransport()
        pipeline = AudioPipeline(cast(Transport, transport), EventBus())
        await pipeline.start()
        service = cast(CallService, type(
            "Service",
            (),
            {"_audio": pipeline, "state": CallState.ACTIVE, "active_call": None},
        )())
        session = CallSession(number="5551234", direction="inbound", service=service)
        setattr(service, "active_call", session)

        with pytest.raises(AudioPipelineError, match="Audio transport produced no audio frames"):
            await session.record(str(tmp_path / "session-silent-recording.wav"), max_duration=0.03)

        await pipeline.stop()

    async def test_record_eof_closes_audio_transport(
        self, pipeline, transport, tmp_path
    ):
        output = tmp_path / "eof-transport-close.wav"
        pipeline._running = True
        transport._open = True
        transport.feed_raw(b"")

        with pytest.raises(AudioPipelineError, match="Audio transport ended during recording"):
            await pipeline.record(str(output), max_duration=0.1)

        assert not pipeline.running
        assert not transport._open

    async def test_record_eof_preserves_existing_output_file(
        self, pipeline, transport, tmp_path
    ):
        output = tmp_path / "existing-recording.wav"
        original = b"existing recording should survive eof"
        output.write_bytes(original)
        pipeline._running = True
        transport.feed_raw(b"")

        with pytest.raises(AudioPipelineError, match="Audio transport ended during recording"):
            await pipeline.record(str(output), max_duration=0.1)

        assert output.read_bytes() == original

    async def test_session_record_propagates_audio_eof_failure(
        self, pipeline, transport, tmp_path
    ):
        pipeline._running = True
        transport.feed_raw(b"")
        service = cast(CallService, type(
            "Service",
            (),
            {"_audio": pipeline, "state": CallState.ACTIVE, "active_call": None},
        )())
        session = CallSession(number="5551234", direction="inbound", service=service)
        setattr(service, "active_call", session)

        with pytest.raises(AudioPipelineError, match="Audio transport ended during recording"):
            await session.record(str(tmp_path / "session-eof-recording.wav"), max_duration=0.1)

    async def test_double_start_is_safe(self, pipeline, transport):
        transport._open = False
        await pipeline.start()
        await pipeline.start()  # Should not raise
        assert pipeline.running

    async def test_double_stop_is_safe(self, pipeline, transport):
        await pipeline.start()
        await pipeline.stop()
        await pipeline.stop()  # Should not raise
        assert not pipeline.running
