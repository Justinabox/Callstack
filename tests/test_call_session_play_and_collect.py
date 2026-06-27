"""Regression tests for CallSession play-and-collect timing."""

import asyncio
from typing import Any, cast

from callstack.events.bus import EventBus
from callstack.events.types import CallState, DTMFEvent
from callstack.voice.service import CallSession


class _FakeAudio:
    def __init__(self):
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.completed = False
        self.cancelled = False

    async def play_file(self, path, cancel=None):
        self.started.set()
        try:
            await self.finish.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.completed = True


class _FakeService:
    def __init__(self):
        self.state = CallState.ACTIVE
        self.active_call: CallSession | None = None
        self._bus = EventBus()
        self._audio = _FakeAudio()


async def _make_running_play_and_collect(timeout: float = 0.02):
    service = _FakeService()
    session = CallSession(number="unknown", direction="inbound", service=cast(Any, service))
    service.active_call = session
    task = asyncio.create_task(
        session.play_and_collect("menu.wav", timeout=timeout, interrupt=True)
    )
    await service._audio.started.wait()
    return service, task


async def test_interruptible_play_and_collect_does_not_cancel_prompt_before_it_finishes():
    service, task = await _make_running_play_and_collect(timeout=0.02)

    await asyncio.sleep(0.04)

    assert not task.done(), "no-input timeout should start after prompt playback"

    service._audio.finish.set()
    result = await asyncio.wait_for(task, timeout=0.1)

    assert result == ""
    assert service._audio.completed is True
    assert service._audio.cancelled is False


async def test_interruptible_play_and_collect_accepts_first_digit_after_prompt_finishes():
    service, task = await _make_running_play_and_collect(timeout=0.02)

    await asyncio.sleep(0.04)
    assert not task.done(), "caller should still get a response window after prompt playback"

    service._audio.finish.set()
    await asyncio.sleep(0)
    await service._bus.emit(DTMFEvent(digit="5"))

    result = await asyncio.wait_for(task, timeout=0.1)

    assert result == "5"
    assert service._audio.completed is True
    assert service._audio.cancelled is False


async def test_interruptible_play_and_collect_digit_during_prompt_cancels_playback():
    service, task = await _make_running_play_and_collect(timeout=0.2)

    await service._bus.emit(DTMFEvent(digit="7"))
    result = await asyncio.wait_for(task, timeout=0.1)

    assert result == "7"
    assert service._audio.cancelled is True
    assert service._audio.completed is False
