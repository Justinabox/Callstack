"""Tests for DTMFCollector."""

import asyncio
import pytest
from callstack.events.bus import EventBus
from callstack.events.types import DTMFEvent
from callstack.voice.dtmf import DTMFCollector


@pytest.fixture
def bus():
    return EventBus()


async def _emit_digits(bus: EventBus, digits: str, delay: float = 0.01):
    """Helper: emit DTMF digits with a small delay between each."""
    for d in digits:
        await asyncio.sleep(delay)
        await bus.emit(DTMFEvent(digit=d))


async def test_collect_single_digit(bus):
    collector = DTMFCollector(bus, max_digits=1, timeout=2.0)
    asyncio.create_task(_emit_digits(bus, "5"))
    result = await collector.collect()
    assert result == "5"


async def test_collect_multiple_digits(bus):
    collector = DTMFCollector(bus, max_digits=4, timeout=2.0)
    asyncio.create_task(_emit_digits(bus, "1234"))
    result = await collector.collect()
    assert result == "1234"


async def test_collect_stops_at_max_digits(bus):
    collector = DTMFCollector(bus, max_digits=3, timeout=2.0)
    asyncio.create_task(_emit_digits(bus, "12345"))
    result = await collector.collect()
    assert result == "123"


async def test_collect_stops_at_terminator(bus):
    collector = DTMFCollector(bus, max_digits=10, timeout=2.0, terminator="#")
    asyncio.create_task(_emit_digits(bus, "42#99"))
    result = await collector.collect()
    assert result == "42"


async def test_collect_timeout_returns_partial(bus):
    collector = DTMFCollector(bus, max_digits=5, timeout=0.1)

    async def emit_slow():
        await bus.emit(DTMFEvent(digit="1"))
        await asyncio.sleep(0.05)
        await bus.emit(DTMFEvent(digit="2"))
        # Then nothing — should timeout

    asyncio.create_task(emit_slow())
    result = await collector.collect()
    assert result == "12"


async def test_collect_timeout_returns_empty(bus):
    collector = DTMFCollector(bus, max_digits=1, timeout=0.05)
    result = await collector.collect()
    assert result == ""


async def test_collect_one_returns_digit(bus):
    collector = DTMFCollector(bus, max_digits=10, timeout=2.0)
    asyncio.create_task(_emit_digits(bus, "7"))
    result = await collector.collect_one()
    assert result == "7"


async def test_collect_one_returns_none_on_timeout(bus):
    collector = DTMFCollector(bus, max_digits=10, timeout=0.05)
    result = await collector.collect_one()
    assert result is None


async def test_collect_overrides_max_digits(bus):
    collector = DTMFCollector(bus, max_digits=10, timeout=2.0)
    asyncio.create_task(_emit_digits(bus, "123456"))
    result = await collector.collect(max_digits=2)
    assert result == "12"


async def test_collect_overrides_timeout(bus):
    collector = DTMFCollector(bus, max_digits=10, timeout=10.0)
    result = await collector.collect(timeout=0.05)
    assert result == ""


async def test_terminator_star(bus):
    collector = DTMFCollector(bus, max_digits=10, timeout=2.0, terminator="*")
    asyncio.create_task(_emit_digits(bus, "99*"))
    result = await collector.collect()
    assert result == "99"


async def test_inter_digit_timeout(bus):
    collector = DTMFCollector(bus, max_digits=10, timeout=5.0, inter_digit_timeout=0.08)

    async def emit_with_gap():
        await bus.emit(DTMFEvent(digit="1"))
        await asyncio.sleep(0.02)
        await bus.emit(DTMFEvent(digit="2"))
        # Long gap — should trigger inter-digit timeout
        await asyncio.sleep(0.2)
        await bus.emit(DTMFEvent(digit="3"))

    asyncio.create_task(emit_with_gap())
    result = await collector.collect()
    assert result == "12"


async def test_multiple_independent_collections(bus):
    """Two sequential collections on the same bus don't leak events."""
    collector = DTMFCollector(bus, max_digits=2, timeout=2.0)

    asyncio.create_task(_emit_digits(bus, "AB"))
    r1 = await collector.collect()
    assert r1 == "AB"

    asyncio.create_task(_emit_digits(bus, "CD"))
    r2 = await collector.collect()
    assert r2 == "CD"
