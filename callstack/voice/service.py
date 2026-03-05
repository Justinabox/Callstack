"""High-level voice call operations: dial, answer, hangup, audio bridge."""

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable

from callstack.events.bus import EventBus
from callstack.events.types import (
    CallState,
    CallStateEvent,
    CallerIDEvent,
    DTMFEvent,
    RingEvent,
)
from callstack.errors import DialError, AnswerError
from callstack.protocol.executor import ATCommandExecutor
from callstack.protocol.commands import ATCommand
from callstack.voice.state import CallStateMachine
from callstack.voice.audio import AudioPipeline
from callstack.voice.dtmf import DTMFCollector

logger = logging.getLogger("callstack.voice.service")


class CallService:
    """High-level voice call operations.

    Wires modem URCs to the call state machine and manages the audio bridge
    lifecycle (PCM registration on call connect, teardown on hangup).
    """

    def __init__(self, executor: ATCommandExecutor, audio: AudioPipeline, bus: EventBus):
        self._at = executor
        self._audio = audio
        self._bus = bus
        self._fsm = CallStateMachine()
        self._active_call: Optional[CallSession] = None
        self._pending_caller: Optional[str] = None

        # Wire URC events to internal handlers
        self._handlers = [
            (RingEvent, self._on_ring),
            (CallerIDEvent, self._on_caller_id),
            (CallStateEvent, self._on_call_state),
        ]
        for event_type, handler in self._handlers:
            bus.subscribe(event_type, handler)

    def close(self) -> None:
        """Unsubscribe all event handlers."""
        for event_type, handler in self._handlers:
            self._bus.unsubscribe(event_type, handler)

    @property
    def state(self) -> CallState:
        return self._fsm.state

    @property
    def active_call(self) -> Optional["CallSession"]:
        return self._active_call

    # -- Outbound --

    async def dial(self, number: str, timeout: float = 30.0) -> "CallSession":
        """Initiate an outbound call. Returns a CallSession handle."""
        await self._fsm.transition(CallState.DIALING)
        logger.info("Dialing %s", number)

        try:
            resp = await self._at.execute(
                ATCommand.dial(number), expect=["OK"], timeout=timeout
            )
        except Exception:
            await self._fsm.transition(CallState.ENDED)
            await self._reset_to_idle()
            raise

        if not resp.success:
            await self._fsm.transition(CallState.ENDED)
            await self._reset_to_idle()
            raise DialError(resp.lines)

        session = CallSession(number=number, direction="outbound", service=self)
        self._active_call = session
        return session

    # -- Inbound --

    async def answer(self) -> "CallSession":
        """Answer an incoming call. Returns a CallSession handle."""
        logger.info("Answering call from %s", self._pending_caller or "unknown")

        resp = await self._at.execute(
            ATCommand.ANSWER, expect=["OK", "VOICE CALL: BEGIN"], timeout=10.0
        )
        if not resp.success:
            raise AnswerError(resp.lines)

        await self._fsm.transition(CallState.ACTIVE)
        await self._enable_audio()

        session = CallSession(
            number=self._pending_caller or "unknown",
            direction="inbound",
            service=self,
        )
        self._active_call = session
        self._pending_caller = None
        return session

    async def hangup(self) -> None:
        """End the current call."""
        logger.info("Hanging up")
        await self._at.execute(
            ATCommand.HANGUP, expect=["OK", "VOICE CALL: END"], timeout=5.0
        )

        if self._fsm.state not in (CallState.ENDED, CallState.IDLE):
            await self._fsm.transition(CallState.ENDED)
        await self._cleanup()

    async def reject(self) -> None:
        """Reject an incoming call without answering."""
        logger.info("Rejecting call")
        await self._at.execute(ATCommand.HANGUP, expect=["OK"], timeout=5.0)
        if self._fsm.state not in (CallState.ENDED, CallState.IDLE):
            await self._fsm.transition(CallState.ENDED)
        await self._cleanup()

    # -- Audio bridge --

    async def _enable_audio(self) -> None:
        """Register PCM audio channel after call connects."""
        logger.debug("Enabling audio bridge")
        resp = await self._at.execute(ATCommand.CPCMREG_ON, expect=["OK"], timeout=5.0)
        if resp.success:
            await self._audio.start()
        else:
            logger.warning("Audio bridge registration failed: %s — call may have no audio", resp.lines)

    async def _disable_audio(self) -> None:
        """Tear down audio bridge."""
        logger.debug("Disabling audio bridge")
        await self._audio.stop()
        await self._at.execute(ATCommand.CPCMREG_OFF, expect=["OK", "ERROR"], timeout=5.0)

    # -- Internal event handlers --

    async def _on_ring(self, event: RingEvent) -> None:
        if self._fsm.state == CallState.IDLE:
            await self._fsm.transition(CallState.RINGING)
            logger.info("Incoming call (RING)")

    async def _on_caller_id(self, event: CallerIDEvent) -> None:
        self._pending_caller = event.number
        logger.info("Caller ID: %s", event.number)

    async def _on_call_state(self, event: CallStateEvent) -> None:
        if event.state == CallState.ACTIVE and self._fsm.state in (
            CallState.DIALING, CallState.RINGING
        ):
            # "VOICE CALL: BEGIN" URC — call connected
            await self._fsm.transition(CallState.ACTIVE)
            if not self._audio.running:
                await self._enable_audio()

        elif event.state == CallState.ENDED and self._fsm.state not in (
            CallState.ENDED, CallState.IDLE
        ):
            # "VOICE CALL: END" or "NO CARRIER" URC — remote hangup
            await self._fsm.transition(CallState.ENDED)
            await self._disable_audio()
            if self._active_call:
                self._active_call._ended.set()
            self._active_call = None
            self._pending_caller = None
            await self._reset_to_idle()

    # -- Cleanup --

    async def _cleanup(self) -> None:
        """Clean up after a call ends."""
        if self._audio.running:
            await self._disable_audio()
        self._active_call = None
        self._pending_caller = None
        await self._reset_to_idle()

    async def _reset_to_idle(self) -> None:
        """Return FSM to IDLE if it's in ENDED state."""
        if self._fsm.state == CallState.ENDED:
            await self._fsm.transition(CallState.IDLE)


@dataclass
class CallSession:
    """Handle for an active call. Provides audio and control methods."""

    number: str
    direction: str  # "inbound" | "outbound"
    service: CallService
    _ended: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    @property
    def is_active(self) -> bool:
        return self.service.state == CallState.ACTIVE

    async def hangup(self) -> None:
        """End this call."""
        await self.service.hangup()
        self._ended.set()

    async def play(self, audio_path: str, cancel: asyncio.Event | None = None) -> None:
        """Play a WAV recording to the caller."""
        await self.service._audio.play_file(audio_path, cancel)

    async def play_sequence(self, paths: list[str], cancel: asyncio.Event | None = None) -> None:
        """Play multiple WAV files back-to-back."""
        await self.service._audio.play_sequence(paths, cancel)

    async def play_loop(self, audio_path: str, cancel: asyncio.Event | None = None) -> None:
        """Loop a WAV file (e.g. hold music) until cancelled."""
        await self.service._audio.play_loop(audio_path, cancel)

    async def record(
        self,
        output_path: str,
        max_duration: float = 60.0,
        stop_on_dtmf: bool = False,
    ) -> str:
        """Record caller audio to a WAV file."""
        return await self.service._audio.record(
            output_path, max_duration, stop_on_dtmf
        )

    async def collect_dtmf(
        self,
        max_digits: int = 1,
        timeout: float = 10.0,
        terminator: str = "#",
        inter_digit_timeout: Optional[float] = None,
    ) -> str:
        """Collect DTMF digits from the caller.

        Args:
            max_digits: Stop after this many digits.
            timeout: Overall timeout in seconds.
            terminator: Digit that ends collection early (excluded from result).
            inter_digit_timeout: Reset deadline after each digit (for variable-length input).
        """
        collector = DTMFCollector(
            self.service._bus, max_digits, timeout, terminator, inter_digit_timeout
        )
        return await collector.collect()

    async def play_and_collect(
        self,
        audio_path: str,
        max_digits: int = 1,
        timeout: float = 10.0,
        terminator: str = "#",
        interrupt: bool = True,
        inter_digit_timeout: Optional[float] = None,
    ) -> str:
        """Play a prompt and collect DTMF input (IVR pattern).

        Args:
            audio_path: WAV file to play as the prompt.
            max_digits: Maximum digits to collect.
            timeout: Seconds to wait for input.
            terminator: Digit that ends collection early.
            interrupt: If True, stop audio playback on first keypress.
            inter_digit_timeout: Reset deadline after each digit.

        Returns:
            Collected digits as a string.
        """
        collector = DTMFCollector(
            self.service._bus, max_digits, timeout, terminator, inter_digit_timeout
        )

        if interrupt:
            # Use a single stream context to avoid losing events between
            # collect_one and collect calls
            async with self.service._bus.stream(DTMFEvent) as events:
                play_task = asyncio.create_task(self.play(audio_path))
                first = await collector.collect_one_from_stream(events, timeout=timeout)
                if first:
                    play_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await play_task
                    if max_digits > 1:
                        remaining = await collector.collect_from_stream(
                            events, max_digits=max_digits - 1, timeout=timeout
                        )
                        return first + remaining
                    return first
                # No digit received during playback — wait for remaining timeout
                play_task.cancel()
                with suppress(asyncio.CancelledError):
                    await play_task
                return ""
        else:
            await self.play(audio_path)
            return await collector.collect()

    async def wait_for_end(self, timeout: float | None = None) -> bool:
        """Wait until the call ends. Returns True if ended, False on timeout."""
        try:
            await asyncio.wait_for(self._ended.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False
