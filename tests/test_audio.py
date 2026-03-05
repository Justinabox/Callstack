"""Tests for AudioPlayer and AudioPipeline."""

import asyncio
import struct
import tempfile
import wave
import os

import pytest
from callstack.transport.mock import MockTransport
from callstack.events.bus import EventBus
from callstack.events.types import DTMFEvent
from callstack.voice.player import AudioPlayer
from callstack.voice.audio import AudioPipeline
from callstack.errors import AudioFormatError


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
