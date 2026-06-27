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
from callstack.privacy import redact_phone_number
from callstack.protocol.executor import ATCommandExecutor
from callstack.protocol.commands import ATCommand
from callstack.voice.state import CallStateMachine
from callstack.voice.audio import AudioPipeline

logger = logging.getLogger("callstack.voice.service")


class CallService:
    """High-level voice call operations.

    Wires modem URCs to the call state machine and manages the audio bridge
    lifecycle (PCM registration on call connect, teardown on hangup).
    """

    def __init__(
        self,
        executor: ATCommandExecutor,
        audio: AudioPipeline,
        bus: EventBus,
        command_timeout: float = 5.0,
    ):
        self._at = executor
        self._audio = audio
        self._bus = bus
        self._command_timeout = command_timeout
        self._fsm = CallStateMachine()
        self._audio_enable_lock = asyncio.Lock()
        self._audio_bridge_registered = False
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
        logger.info("Dialing %s", redact_phone_number(number))

        try:
            resp = await self._at.execute(
                ATCommand.dial(number), expect=["OK"], timeout=timeout
            )
        except Exception:
            await self._cleanup_failed_dial()
            raise

        if not resp.success:
            await self._cleanup_failed_dial()
            raise DialError(resp.lines)

        session = CallSession(number=number, direction="outbound", service=self)
        self._active_call = session
        return session

    # -- Inbound --

    async def answer(self) -> "CallSession":
        """Answer an incoming call. Returns a CallSession handle."""
        logger.info("Answering call from %s", redact_phone_number(self._pending_caller))

        try:
            resp = await self._at.execute(
                ATCommand.ANSWER, expect=["OK", "VOICE CALL: BEGIN"], timeout=10.0
            )
        except Exception:
            await self._cleanup_failed_answer()
            raise
        if not resp.success:
            await self._cleanup_failed_answer()
            raise AnswerError(resp.lines)

        if self._fsm.state != CallState.ACTIVE:
            await self._fsm.transition(CallState.ACTIVE)
        await self._ensure_audio_enabled()

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
            ATCommand.HANGUP,
            expect=["OK", "VOICE CALL: END"],
            timeout=self._command_timeout,
        )

        if self._fsm.state not in (CallState.ENDED, CallState.IDLE):
            await self._fsm.transition(CallState.ENDED)
        await self._cleanup()

    async def reject(self) -> None:
        """Reject an incoming call without answering."""
        logger.info("Rejecting call")
        await self._at.execute(
            ATCommand.HANGUP, expect=["OK"], timeout=self._command_timeout
        )
        if self._fsm.state not in (CallState.ENDED, CallState.IDLE):
            await self._fsm.transition(CallState.ENDED)
        await self._cleanup()

    # -- Audio bridge --

    async def _ensure_audio_enabled(self) -> None:
        """Enable the audio bridge at most once across concurrent connect paths."""
        async with self._audio_enable_lock:
            if self._fsm.state != CallState.ACTIVE:
                return
            if not self._audio.running and not self._audio_bridge_registered:
                await self._enable_audio()

    async def _enable_audio(self) -> None:
        """Register PCM audio channel after call connects."""
        logger.debug("Enabling audio bridge")
        try:
            resp = await self._at.execute(
                ATCommand.CPCMREG_ON, expect=["OK"], timeout=self._command_timeout
            )
        except Exception as exc:
            logger.warning("Audio bridge registration failed: %s — call may have no audio", exc)
            return
        if resp.success:
            self._audio_bridge_registered = True
            try:
                await self._audio.start()
            except Exception as exc:
                logger.warning("Audio pipeline start failed: %s — call may have no audio", exc)
        else:
            logger.warning("Audio bridge registration failed: %s — call may have no audio", resp.lines)

    async def _disable_audio(self) -> None:
        """Tear down audio bridge."""
        logger.debug("Disabling audio bridge")
        if self._audio.running:
            await self._audio.stop()
        if self._audio_bridge_registered:
            try:
                await self._at.execute(
                    ATCommand.CPCMREG_OFF,
                    expect=["OK", "ERROR"],
                    timeout=self._command_timeout,
                )
            finally:
                self._audio_bridge_registered = False

    # -- Internal event handlers --

    async def _on_ring(self, event: RingEvent) -> None:
        if self._fsm.state == CallState.IDLE:
            await self._fsm.transition(CallState.RINGING)
            logger.info("Incoming call (RING)")

    async def _on_caller_id(self, event: CallerIDEvent) -> None:
        self._pending_caller = event.number
        logger.info("Caller ID: %s", redact_phone_number(event.number))

    async def _on_call_state(self, event: CallStateEvent) -> None:
        if event.state == CallState.ACTIVE and self._fsm.state in (
            CallState.DIALING, CallState.RINGING
        ):
            # "VOICE CALL: BEGIN" URC — call connected
            await self._fsm.transition(CallState.ACTIVE)
            await self._ensure_audio_enabled()

        elif event.state == CallState.ENDED and self._fsm.state not in (
            CallState.ENDED, CallState.IDLE
        ):
            # "VOICE CALL: END" or "NO CARRIER" URC — remote hangup
            await self._fsm.transition(CallState.ENDED)
            async with self._audio_enable_lock:
                await self._disable_audio()
            if self._active_call:
                self._active_call._ended.set()
            self._active_call = None
            self._pending_caller = None
            await self._reset_to_idle()

    # -- Cleanup --

    async def _cleanup(self) -> None:
        """Clean up after a call ends."""
        async with self._audio_enable_lock:
            if self._audio.running or self._audio_bridge_registered:
                await self._disable_audio()
        self._active_call = None
        self._pending_caller = None
        await self._reset_to_idle()

    async def _cleanup_failed_dial(self) -> None:
        """Clean up an outbound dial attempt that failed before a session existed."""
        if self._fsm.state not in (CallState.ENDED, CallState.IDLE):
            await self._fsm.transition(CallState.ENDED)
        async with self._audio_enable_lock:
            if self._audio.running or self._audio_bridge_registered:
                try:
                    await self._disable_audio()
                except Exception as exc:
                    logger.debug("Audio cleanup after failed dial failed: %s", exc)
        self._active_call = None
        await self._reset_to_idle()

    async def _cleanup_failed_answer(self) -> None:
        """Clean up an inbound call attempt that failed before a session existed."""
        if self._fsm.state not in (CallState.ENDED, CallState.IDLE):
            await self._fsm.transition(CallState.ENDED)
        async with self._audio_enable_lock:
            if self._audio.running or self._audio_bridge_registered:
                try:
                    await self._disable_audio()
                except Exception as exc:
                    logger.debug("Audio cleanup after failed answer failed: %s", exc)
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
        return self.service.state == CallState.ACTIVE and self.service.active_call is self

    def _require_active_call(self, action: str) -> None:
        if not self.is_active:
            raise RuntimeError(f"Cannot {action} without an active call")

    def _require_current_call(self, action: str) -> None:
        if (
            self.service.active_call is not self
            or self.service.state in (CallState.IDLE, CallState.ENDED)
        ):
            raise RuntimeError(f"Cannot {action} without an active call")

    async def _next_dtmf_event_while_active(
        self, events, timeout: float | None
    ) -> DTMFEvent | None:
        now = asyncio.get_running_loop().time
        deadline = None if timeout is None else now() + timeout
        while True:
            self._require_active_call("collect DTMF")
            if deadline is None:
                wait_timeout = 0.05
            else:
                remaining = deadline - now()
                if remaining <= 0:
                    return None
                wait_timeout = min(remaining, 0.05)
            event = await events.next(timeout=wait_timeout)
            if event is None:
                continue
            self._require_active_call("collect DTMF")
            if isinstance(event, DTMFEvent):
                return event

    async def _collect_dtmf_from_stream_while_active(
        self,
        events,
        max_digits: int,
        timeout: float,
        terminator: str,
        inter_digit_timeout: Optional[float] = None,
    ) -> str:
        digits: list[str] = []
        now = asyncio.get_running_loop().time
        overall_deadline = now() + timeout
        deadline = overall_deadline

        while len(digits) < max_digits:
            remaining = deadline - now()
            if remaining <= 0:
                break
            event = await self._next_dtmf_event_while_active(events, remaining)
            if event is None:
                break
            digit = event.digit
            if digit == terminator:
                break
            digits.append(digit)
            if inter_digit_timeout is not None:
                deadline = min(overall_deadline, now() + inter_digit_timeout)

        return "".join(digits)

    async def hangup(self) -> None:
        """End this call."""
        self._require_current_call("hang up")
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
        if not self.is_active:
            raise RuntimeError("Cannot record without an active call")
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
        self._require_active_call("collect DTMF")
        async with self.service._bus.stream(DTMFEvent) as events:
            return await self._collect_dtmf_from_stream_while_active(
                events, max_digits, timeout, terminator, inter_digit_timeout
            )

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
        self._require_active_call("play and collect DTMF")

        if interrupt:
            # Use a single stream context to avoid losing events between the
            # first keypress race and any remaining digit collection. The
            # no-input timeout starts after prompt playback unless a digit
            # arrives early and interrupts the prompt.
            async with self.service._bus.stream(DTMFEvent) as events:
                play_task = asyncio.create_task(self.play(audio_path))
                first_event_task = asyncio.create_task(
                    self._next_dtmf_event_while_active(events, timeout=None)
                )

                def event_digit(event: object) -> str | None:
                    if isinstance(event, DTMFEvent) and event.digit != terminator:
                        return event.digit
                    return None

                async def finish_collection(first: str | None) -> str:
                    if not first:
                        return ""
                    if max_digits > 1:
                        remaining = await self._collect_dtmf_from_stream_while_active(
                            events,
                            max_digits=max_digits - 1,
                            timeout=timeout,
                            terminator=terminator,
                            inter_digit_timeout=inter_digit_timeout,
                        )
                        return first + remaining
                    return first

                try:
                    done, _ = await asyncio.wait(
                        {play_task, first_event_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    if first_event_task in done:
                        event = first_event_task.result()
                        first = event_digit(event)
                        play_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await play_task
                        return await finish_collection(first)

                    await play_task
                    try:
                        event = await asyncio.wait_for(first_event_task, timeout=timeout)
                    except asyncio.TimeoutError:
                        first_event_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await first_event_task
                        return ""
                    first = event_digit(event)
                    return await finish_collection(first)
                finally:
                    if not first_event_task.done():
                        first_event_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await first_event_task
                    if not play_task.done():
                        play_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await play_task
        else:
            await self.play(audio_path)
            self._require_active_call("play and collect DTMF")
            async with self.service._bus.stream(DTMFEvent) as events:
                return await self._collect_dtmf_from_stream_while_active(
                    events, max_digits, timeout, terminator, inter_digit_timeout
                )

    async def send_dtmf(
        self,
        digits: str,
        duration_ms: int = 100,
        inter_digit_delay_ms: int = 0,
    ) -> None:
        """Send DTMF tones during an active call.

        Args:
            digits: One or more DTMF digits (0-9, *, #, A-D).
            duration_ms: Tone duration in milliseconds. Encoded in the
                AT+VTS duration field as tenths of a second; use 0 to request
                the modem default. Non-zero values must be 100-25500 ms in
                100 ms increments.
            inter_digit_delay_ms: Optional local delay between DTMF commands;
                this is separate from modem-side tone duration.
        """
        if type(inter_digit_delay_ms) is not int or inter_digit_delay_ms < 0:
            raise ValueError(
                "DTMF inter-digit delay must be a non-negative integer number of milliseconds"
            )
        if not self.is_active:
            raise RuntimeError("Cannot send DTMF without an active call")
        for index, digit in enumerate(digits):
            if not self.is_active:
                raise RuntimeError("Cannot send DTMF without an active call")
            await self.service._at.execute(
                ATCommand.send_dtmf(digit, duration_ms=duration_ms),
                expect=["OK"],
                timeout=getattr(self.service, "_command_timeout", 5.0),
            )
            if index < len(digits) - 1 and inter_digit_delay_ms:
                await asyncio.sleep(inter_digit_delay_ms / 1000.0)

    async def wait_for_end(self, timeout: float | None = None) -> bool:
        """Wait until the call ends. Returns True if ended, False on timeout."""
        try:
            await asyncio.wait_for(self._ended.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False
