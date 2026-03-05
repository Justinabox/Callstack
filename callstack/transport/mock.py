import asyncio
from collections import deque

from callstack.transport.base import Transport


class MockTransport(Transport):
    """Mock transport for testing without hardware.

    Supports scripted responses: feed lines that will be returned
    by readline() in order. Captures all written data for assertions.
    """

    def __init__(self):
        self._open = False
        self._responses: deque[bytes] = deque()
        self._written: list[bytes] = []
        self._response_event = asyncio.Event()

    async def open(self) -> None:
        self._open = True

    async def close(self) -> None:
        self._open = False

    async def write(self, data: bytes) -> None:
        self._written.append(data)

    async def read(self, size: int = -1) -> bytes:
        while not self._responses:
            self._response_event.clear()
            await self._response_event.wait()
        return self._responses.popleft()

    async def readline(self) -> bytes:
        while not self._responses:
            self._response_event.clear()
            await self._response_event.wait()
        return self._responses.popleft()

    def in_waiting(self) -> int:
        return len(self._responses)

    # --- Test helpers ---

    def feed(self, *lines: str) -> None:
        """Queue response lines. Each string gets \\r\\n appended and encoded."""
        for line in lines:
            self._responses.append(f"{line}\r\n".encode())
        self._response_event.set()

    def feed_raw(self, data: bytes) -> None:
        """Queue raw bytes (for audio transport testing)."""
        self._responses.append(data)
        self._response_event.set()

    @property
    def last_written(self) -> str:
        """Return the last written data decoded as string."""
        if not self._written:
            return ""
        return self._written[-1].decode("ascii", errors="replace")

    @property
    def all_written(self) -> list[str]:
        """Return all written data decoded as strings."""
        return [d.decode("ascii", errors="replace") for d in self._written]

    def clear(self) -> None:
        """Reset all state."""
        self._responses.clear()
        self._written.clear()
