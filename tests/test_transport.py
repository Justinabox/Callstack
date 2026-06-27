"""Tests for transport adapters."""

import asyncio

import pytest

from callstack.errors import TransportError
from callstack.transport.mock import MockTransport
from callstack.transport.serial import SerialTransport


@pytest.fixture
def transport():
    return MockTransport()


async def test_open_close(transport):
    await transport.open()
    assert transport._open is True
    await transport.close()
    assert transport._open is False


async def test_feed_and_readline(transport):
    transport.feed("OK")
    data = await transport.readline()
    assert data == b"OK\r\n"


async def test_feed_multiple(transport):
    transport.feed("+CSQ: 20,0", "OK")
    line1 = await transport.readline()
    line2 = await transport.readline()
    assert b"+CSQ: 20,0" in line1
    assert b"OK" in line2


async def test_write_captures(transport):
    await transport.write(b"AT\r\n")
    assert transport.last_written == "AT\r\n"
    assert len(transport.all_written) == 1


async def test_write_multiple(transport):
    await transport.write(b"AT\r\n")
    await transport.write(b"AT+CSQ\r\n")
    assert len(transport.all_written) == 2
    assert transport.all_written[0] == "AT\r\n"
    assert transport.all_written[1] == "AT+CSQ\r\n"


async def test_in_waiting(transport):
    assert transport.in_waiting() == 0
    transport.feed("OK")
    assert transport.in_waiting() == 1


async def test_clear(transport):
    transport.feed("OK")
    await transport.write(b"AT\r\n")
    transport.clear()
    assert transport.in_waiting() == 0
    assert len(transport.all_written) == 0


async def test_feed_raw(transport):
    transport.feed_raw(b"\x00\x01\x02")
    data = await transport.read(3)
    assert data == b"\x00\x01\x02"


async def test_serial_readline_raises_transport_error_on_initial_eof():
    """A serial EOF before any line bytes is a disconnect, not a blank modem line."""
    reader = asyncio.StreamReader()
    reader.feed_eof()
    transport = SerialTransport("/dev/ttyUSB-test")
    transport._reader = reader

    with pytest.raises(TransportError, match="closed|EOF"):
        await transport.readline()
