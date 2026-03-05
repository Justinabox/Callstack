import asyncio
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Callable, Optional, TypeVar

from callstack.events.types import Event

logger = logging.getLogger("callstack.events.bus")

T = TypeVar("T", bound=Event)


class EventBus:
    """Typed async event bus with pub/sub and async iteration."""

    def __init__(self):
        self._subscribers: dict[type, list[Callable]] = defaultdict(list)
        self._queues: dict[type, list[asyncio.Queue]] = defaultdict(list)
        self._tasks: set[asyncio.Task] = set()

    def on(self, event_type: type[T]):
        """Decorator to subscribe a coroutine to an event type."""
        def decorator(fn: Callable) -> Callable:
            self._subscribers[event_type].append(fn)
            return fn
        return decorator

    def subscribe(self, event_type: type[T], handler: Callable) -> None:
        """Imperatively subscribe a handler to an event type."""
        self._subscribers[event_type].append(handler)

    def unsubscribe(self, event_type: type[T], handler: Callable) -> None:
        """Remove a handler subscription."""
        subs = self._subscribers.get(event_type, [])
        if handler in subs:
            subs.remove(handler)

    async def emit(self, event: Event) -> None:
        """Emit an event to all subscribers and queues."""
        for fn in list(self._subscribers.get(type(event), [])):
            task = asyncio.create_task(fn(event))
            self._tasks.add(task)
            task.add_done_callback(self._task_done)
        for q in list(self._queues.get(type(event), [])):
            await q.put(event)

    def _task_done(self, task: asyncio.Task) -> None:
        """Handle completed subscriber tasks: log exceptions, then discard."""
        self._tasks.discard(task)
        if not task.cancelled():
            exc = task.exception()
            if exc:
                logger.error("Event subscriber raised %s: %s", type(exc).__name__, exc)

    @asynccontextmanager
    async def stream(self, event_type: type[T]):
        """Async context manager yielding an EventStream for the given type.

        Usage:
            async with bus.stream(DTMFEvent) as events:
                async for event in events:
                    print(event.digit)
        """
        q: asyncio.Queue[Event] = asyncio.Queue()
        self._queues[event_type].append(q)
        try:
            yield EventStream(q)
        finally:
            try:
                self._queues[event_type].remove(q)
            except ValueError:
                pass


class EventStream:
    """Async iterator wrapper around a queue."""

    def __init__(self, queue: asyncio.Queue):
        self._queue = queue

    def __aiter__(self):
        return self

    async def __anext__(self) -> Event:
        return await self._queue.get()

    async def next(self, timeout: Optional[float] = None) -> Optional[Event]:
        """Get next event with optional timeout. Returns None on timeout."""
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            return None
