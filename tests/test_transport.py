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


async def test_serial_readline_rejects_partial_line_on_eof():
    """An unterminated partial frame followed by EOF is a truncated read, not a valid line."""
    reader = asyncio.StreamReader()
    reader.feed_data(b"+CSQ: 20,0")  # no trailing newline, not an SMS prompt
    reader.feed_eof()
    transport = SerialTransport("/dev/ttyUSB-test")
    transport._reader = reader

    with pytest.raises(TransportError, match="closed|EOF"):
        await transport.readline()


async def test_serial_readline_partial_eof_does_not_leak_buffer_contents():
    """The truncated-EOF error must flag the condition without echoing buffered bytes,
    which can hold SMS bodies, phone numbers, USSD, or SIM data that may reach logs."""
    sentinel = b"SENTINEL-SENSITIVE-PAYLOAD-0000-DO-NOT-LOG"
    reader = asyncio.StreamReader()
    reader.feed_data(sentinel)  # no trailing newline, not an SMS prompt
    reader.feed_eof()
    transport = SerialTransport("/dev/ttyUSB-test")
    transport._reader = reader

    with pytest.raises(TransportError) as exc_info:
        await transport.readline()

    message = str(exc_info.value)
    # Message must identify the truncated / partial EOF condition...
    assert "partial" in message.lower() or "truncated" in message.lower()
    # ...but must NOT include the raw buffered content in any form.
    assert "SENTINEL" not in message
    assert sentinel.decode() not in message
    assert repr(sentinel) not in message
    assert repr(bytearray(sentinel)) not in message
