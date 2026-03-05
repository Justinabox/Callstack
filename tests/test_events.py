"""Tests for the event bus and event types."""

import asyncio
import pytest
from callstack.events.bus import EventBus
from callstack.events.types import (
    DTMFEvent,
    RingEvent,
    CallStateEvent,
    CallState,
    IncomingSMSEvent,
)


@pytest.fixture
def bus():
    return EventBus()


async def test_subscribe_and_emit(bus):
    received = []

    @bus.on(RingEvent)
    async def handler(event):
        received.append(event)

    await bus.emit(RingEvent())
    await asyncio.sleep(0.01)  # Let the created task run

    assert len(received) == 1
    assert isinstance(received[0], RingEvent)


async def test_emit_only_to_matching_type(bus):
    ring_received = []
    dtmf_received = []

    @bus.on(RingEvent)
    async def on_ring(event):
        ring_received.append(event)

    @bus.on(DTMFEvent)
    async def on_dtmf(event):
        dtmf_received.append(event)

    await bus.emit(DTMFEvent(digit="5"))
    await asyncio.sleep(0.01)

    assert len(ring_received) == 0
    assert len(dtmf_received) == 1
    assert dtmf_received[0].digit == "5"


async def test_subscribe_imperative(bus):
    received = []

    async def handler(event):
        received.append(event)

    bus.subscribe(RingEvent, handler)
    await bus.emit(RingEvent())
    await asyncio.sleep(0.01)

    assert len(received) == 1


async def test_unsubscribe(bus):
    received = []

    async def handler(event):
        received.append(event)

    bus.subscribe(RingEvent, handler)
    bus.unsubscribe(RingEvent, handler)
    await bus.emit(RingEvent())
    await asyncio.sleep(0.01)

    assert len(received) == 0


async def test_stream(bus):
    async with bus.stream(DTMFEvent) as stream:
        await bus.emit(DTMFEvent(digit="1"))
        await bus.emit(DTMFEvent(digit="2"))

        e1 = await stream.next(timeout=1.0)
        e2 = await stream.next(timeout=1.0)

        assert e1.digit == "1"
        assert e2.digit == "2"


async def test_stream_timeout(bus):
    async with bus.stream(DTMFEvent) as stream:
        result = await stream.next(timeout=0.05)
        assert result is None


async def test_stream_cleanup(bus):
    """After exiting stream context, queue is removed."""
    async with bus.stream(DTMFEvent):
        assert len(bus._queues[DTMFEvent]) == 1

    assert len(bus._queues[DTMFEvent]) == 0


async def test_multiple_subscribers(bus):
    results = {"a": [], "b": []}

    @bus.on(RingEvent)
    async def handler_a(event):
        results["a"].append(event)

    @bus.on(RingEvent)
    async def handler_b(event):
        results["b"].append(event)

    await bus.emit(RingEvent())
    await asyncio.sleep(0.01)

    assert len(results["a"]) == 1
    assert len(results["b"]) == 1


async def test_event_frozen():
    """Events should be immutable."""
    event = DTMFEvent(digit="5")
    with pytest.raises(AttributeError):
        event.digit = "6"


async def test_event_has_timestamp():
    event = RingEvent()
    assert event.timestamp is not None
