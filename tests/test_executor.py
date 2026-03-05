"""Tests for the AT command executor."""

import asyncio
import pytest
from callstack.events.bus import EventBus
from callstack.events.types import RingEvent, DTMFEvent
from callstack.errors import ATTimeoutError
from callstack.protocol.executor import ATCommandExecutor
from callstack.protocol.urc import URCDispatcher
from callstack.transport.mock import MockTransport


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def transport():
    return MockTransport()


@pytest.fixture
def urc(bus):
    return URCDispatcher(bus)


@pytest.fixture
async def executor(transport, urc):
    ex = ATCommandExecutor(transport, urc)
    await ex.start_reader()
    yield ex
    await ex.stop_reader()


async def test_simple_at_ok(executor, transport):
    """AT -> OK"""
    transport.feed("OK")
    resp = await executor.execute("AT")
    assert resp.success is True
    assert "OK" in resp.lines
    assert transport.last_written == "AT\r\n"


async def test_signal_quality(executor, transport):
    """AT+CSQ -> +CSQ: 20,0 / OK"""
    transport.feed("+CSQ: 20,0", "OK")
    resp = await executor.execute("AT+CSQ")
    assert resp.success is True
    assert "+CSQ: 20,0" in resp.lines
    assert "OK" in resp.lines


async def test_data_lines(executor, transport):
    """data_lines excludes the final result code."""
    transport.feed("+CSQ: 20,0", "OK")
    resp = await executor.execute("AT+CSQ")
    assert resp.data_lines == ["+CSQ: 20,0"]


async def test_error_response(executor, transport):
    """AT command returning ERROR."""
    transport.feed("ERROR")
    resp = await executor.execute("AT+INVALID")
    assert resp.success is False


async def test_cme_error(executor, transport):
    """+CME ERROR response."""
    transport.feed("+CME ERROR: 10")
    resp = await executor.execute("AT+COPS=?")
    assert resp.success is False
    assert "+CME ERROR: 10" in resp.lines


async def test_timeout(executor, transport):
    """No response within timeout raises ATTimeoutError."""
    with pytest.raises(ATTimeoutError):
        await executor.execute("AT", timeout=0.1)


async def test_echo_suppression(executor, transport):
    """If modem echoes back the command, it should be skipped."""
    transport.feed("AT+CSQ", "+CSQ: 15,0", "OK")
    resp = await executor.execute("AT+CSQ")
    assert resp.success is True
    # The echoed command should not be in the response lines
    assert "AT+CSQ" not in resp.lines
    assert "+CSQ: 15,0" in resp.lines


async def test_urc_during_command(executor, transport, bus):
    """URCs received during command execution are dispatched, not included in response."""
    received_urcs = []

    @bus.on(RingEvent)
    async def on_ring(event):
        received_urcs.append(event)

    transport.feed("RING", "+CSQ: 20,0", "OK")
    resp = await executor.execute("AT+CSQ")

    assert resp.success is True
    # RING should not be in command response
    assert "RING" not in resp.lines
    assert "+CSQ: 20,0" in resp.lines

    await asyncio.sleep(0.01)  # Let the URC handler task run
    assert len(received_urcs) == 1


async def test_blank_lines_skipped(executor, transport):
    """Blank lines between response lines are ignored."""
    transport.feed("", "+CSQ: 20,0", "", "OK")
    resp = await executor.execute("AT+CSQ")
    assert resp.success is True
    assert resp.data_lines == ["+CSQ: 20,0"]


async def test_custom_expect(executor, transport):
    """Custom expected result code (e.g. > for SMS)."""
    transport.feed("> ")
    resp = await executor.execute('AT+CMGS="+1555"', expect=[">"])
    assert resp.success is True


async def test_serialization(executor, transport):
    """Commands are serialized -- only one executes at a time."""
    order = []

    async def feeder():
        """Feed responses with timing that ensures the reader routes them correctly."""
        for _ in range(2):
            await asyncio.sleep(0.02)
            transport.feed("OK")

    asyncio.create_task(feeder())

    async def run_cmd(cmd):
        resp = await executor.execute(cmd, timeout=2.0)
        order.append(cmd)
        return resp

    # Start two commands concurrently — executor lock serializes them
    t1 = asyncio.create_task(run_cmd("AT"))
    t2 = asyncio.create_task(run_cmd("AT+CSQ"))

    r1, r2 = await asyncio.gather(t1, t2)
    assert r1.success is True
    assert r2.success is True
    assert len(order) == 2
