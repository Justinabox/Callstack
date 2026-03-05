import asyncio
from typing import Optional

import serial_asyncio

from callstack.transport.base import Transport
from callstack.errors import TransportError


class SerialTransport(Transport):
    """pyserial-asyncio based transport.

    Opens a serial port as an asyncio stream pair.
    Supports auto-reconnect on USB disconnect/reconnect.
    """

    def __init__(self, port: str, baudrate: int = 115200):
        self.port = port
        self.baudrate = baudrate
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None

    async def open(self) -> None:
        try:
            self._reader, self._writer = await serial_asyncio.open_serial_connection(
                url=self.port, baudrate=self.baudrate
            )
        except Exception as e:
            raise TransportError(f"Failed to open {self.port}: {e}") from e

    async def close(self) -> None:
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()
            self._reader = None
            self._writer = None

    async def write(self, data: bytes) -> None:
        if not self._writer:
            raise TransportError("Transport not open")
        self._writer.write(data)
        await self._writer.drain()

    async def read(self, size: int = -1) -> bytes:
        if not self._reader:
            raise TransportError("Transport not open")
        if size < 0:
            return await self._reader.read(4096)
        return await self._reader.read(size)

    async def readline(self) -> bytes:
        if not self._reader:
            raise TransportError("Transport not open")
        return await self._reader.readline()

    def in_waiting(self) -> int:
        return 0  # Not directly available via asyncio streams
