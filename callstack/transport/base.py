from abc import ABC, abstractmethod


class Transport(ABC):
    """Async byte stream interface to modem hardware."""

    @abstractmethod
    async def open(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def write(self, data: bytes) -> None: ...

    @abstractmethod
    async def read(self, size: int = -1) -> bytes: ...

    @abstractmethod
    async def readline(self) -> bytes: ...

    @abstractmethod
    def in_waiting(self) -> int: ...
