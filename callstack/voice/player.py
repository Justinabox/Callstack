"""WAV file loading, validation, and streaming over audio transport."""

import asyncio
import wave
import logging

from callstack.transport.base import Transport
from callstack.errors import AudioFormatError

logger = logging.getLogger("callstack.voice.player")


class AudioPlayer:
    """Loads, validates, and streams WAV files over the audio transport.

    All audio MUST be: 8000 Hz, 16-bit signed LE, mono.
    Files that don't match are rejected at load time (not silently resampled).

    Supports:
    - Single file playback (blocking until complete or cancelled)
    - Queued sequential playback (play a list of files back-to-back)
    - Looped playback (e.g. hold music)
    - Interruptible playback (cancel on DTMF or external signal)
    """

    REQUIRED_RATE = 8000
    REQUIRED_CHANNELS = 1
    REQUIRED_SAMPLE_WIDTH = 2  # 16-bit
    CHUNK_FRAMES = 320  # 40ms chunks at 8kHz

    def __init__(self, transport: Transport):
        self._transport = transport

    def validate(self, path: str) -> None:
        """Check WAV file matches modem audio format. Raises AudioFormatError on mismatch."""
        with wave.open(path, "rb") as wf:
            if wf.getframerate() != self.REQUIRED_RATE:
                raise AudioFormatError(
                    f"{path}: sample rate {wf.getframerate()}, need {self.REQUIRED_RATE}"
                )
            if wf.getnchannels() != self.REQUIRED_CHANNELS:
                raise AudioFormatError(
                    f"{path}: {wf.getnchannels()} channels, need {self.REQUIRED_CHANNELS}"
                )
            if wf.getsampwidth() != self.REQUIRED_SAMPLE_WIDTH:
                raise AudioFormatError(
                    f"{path}: sample width {wf.getsampwidth()}, need {self.REQUIRED_SAMPLE_WIDTH}"
                )

    async def play(self, path: str, cancel: asyncio.Event | None = None) -> None:
        """Stream a WAV file to the audio transport in real-time.

        Paces output at the audio sample rate so the modem receives data
        at the correct speed. Blocks until the file finishes or cancel is set.
        """
        self.validate(path)
        chunk_duration = self.CHUNK_FRAMES / self.REQUIRED_RATE

        with wave.open(path, "rb") as wf:
            logger.debug("Playing: %s (%d frames)", path, wf.getnframes())
            while True:
                if cancel and cancel.is_set():
                    logger.debug("Playback cancelled: %s", path)
                    break

                data = wf.readframes(self.CHUNK_FRAMES)
                if not data:
                    break

                await self._transport.write(data)
                # Pace output to match real-time audio rate
                await asyncio.sleep(chunk_duration)

        logger.debug("Playback complete: %s", path)

    async def play_sequence(self, paths: list[str], cancel: asyncio.Event | None = None) -> None:
        """Play multiple WAV files back-to-back."""
        for path in paths:
            if cancel and cancel.is_set():
                break
            await self.play(path, cancel)

    async def play_loop(self, path: str, cancel: asyncio.Event | None = None) -> None:
        """Loop a WAV file until cancel is set (e.g. hold music)."""
        if cancel is None:
            raise ValueError("play_loop requires a cancel Event to stop the loop")
        self.validate(path)
        while not cancel.is_set():
            await self.play(path, cancel)
