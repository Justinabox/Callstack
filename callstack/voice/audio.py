"""Audio pipeline: manages PCM streaming over the modem's audio serial port."""

import asyncio
import math
import os
import tempfile
import wave
import logging

from callstack.transport.base import Transport
from callstack.events.bus import EventBus
from callstack.events.types import DTMFEvent
from callstack.errors import AudioPipelineError
from callstack.voice.player import AudioPlayer

logger = logging.getLogger("callstack.voice.audio")


class AudioPipeline:
    """Manages PCM audio streaming over the modem's audio serial port.

    Audio format: 8000 Hz, 16-bit signed LE, mono (standard GSM).

    Owns the audio transport lifecycle and delegates playback to AudioPlayer.
    Recording reads raw PCM from the transport and writes to WAV files.
    """

    SAMPLE_RATE = 8000
    SAMPLE_WIDTH = 2  # bytes (16-bit)
    CHANNELS = 1
    CHUNK_BYTES = 640  # 320 frames * 2 bytes = 40ms of audio

    def __init__(self, transport: Transport, bus: EventBus):
        self._transport = transport
        self._bus = bus
        self._player = AudioPlayer(transport)
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        """Open the audio transport and mark the pipeline as active."""
        if not self._running:
            await self._transport.open()
            self._running = True
            logger.info("Audio pipeline started")

    async def stop(self) -> None:
        """Stop the pipeline and close the audio transport."""
        if self._running:
            self._running = False
            await self._transport.close()
            logger.info("Audio pipeline stopped")

    async def play_file(self, path: str, cancel: asyncio.Event | None = None) -> None:
        """Stream a WAV recording to the modem audio port."""
        await self._player.play(path, cancel)

    async def play_sequence(self, paths: list[str], cancel: asyncio.Event | None = None) -> None:
        """Play multiple WAV files back-to-back."""
        await self._player.play_sequence(paths, cancel)

    async def play_loop(self, path: str, cancel: asyncio.Event | None = None) -> None:
        """Loop a WAV file (e.g. hold music) until cancelled."""
        await self._player.play_loop(path, cancel)

    async def record(
        self,
        output_path: str,
        max_duration: float = 60.0,
        stop_on_dtmf: bool = False,
    ) -> str:
        """Record incoming audio to a WAV file.

        Returns the output path when recording finishes (on timeout,
        DTMF interrupt, or pipeline stop).
        """
        if not self._running:
            raise AudioPipelineError(
                "Audio pipeline is not running; start audio before recording"
            )
        if not math.isfinite(max_duration) or max_duration <= 0:
            raise AudioPipelineError("max_duration must be positive and finite")

        stop = asyncio.Event()

        if stop_on_dtmf:
            async def _on_dtmf(_: DTMFEvent) -> None:
                stop.set()
            self._bus.subscribe(DTMFEvent, _on_dtmf)

        output_path_str = os.fspath(output_path)
        output_dir = os.path.dirname(os.path.abspath(output_path_str))
        output_name = os.path.basename(output_path_str)
        fd, recording_path = tempfile.mkstemp(
            prefix=f".{output_name}.", suffix=".tmp", dir=output_dir
        )
        os.close(fd)
        recording_succeeded = False
        try:
            with wave.open(recording_path, "wb") as wf:
                wf.setnchannels(self.CHANNELS)
                wf.setsampwidth(self.SAMPLE_WIDTH)
                wf.setframerate(self.SAMPLE_RATE)

                logger.info("Recording to %s (max %.1fs)", output_path, max_duration)
                loop = asyncio.get_running_loop()
                deadline = loop.time() + max_duration

                while self._running and not stop.is_set():
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        break

                    try:
                        data = await asyncio.wait_for(
                            self._transport.read(self.CHUNK_BYTES),
                            timeout=min(0.1, remaining),
                        )
                        if data == b"":
                            was_running = self._running
                            self._running = False
                            if was_running:
                                try:
                                    await self._transport.close()
                                except Exception:
                                    logger.warning(
                                        "Audio transport close failed after recording EOF"
                                    )
                            raise AudioPipelineError(
                                "Audio transport ended during recording"
                            )
                        wf.writeframes(data)
                    except asyncio.TimeoutError:
                        continue

            os.replace(recording_path, output_path_str)
            recording_succeeded = True
            logger.info("Recording complete: %s", output_path)
        finally:
            if not recording_succeeded:
                try:
                    os.remove(recording_path)
                except OSError:
                    pass
            if stop_on_dtmf:
                self._bus.unsubscribe(DTMFEvent, _on_dtmf)

        return output_path
