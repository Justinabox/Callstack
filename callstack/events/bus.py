import asyncio
import inspect
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, Optional, TypeVar, cast

from callstack.events.types import Event

logger = logging.getLogger("callstack.events.bus")

T = TypeVar("T", bound=Event)
HandlerResult = Awaitable[None] | None
EventHandler = Callable[[Event], HandlerResult]


class EventBus:
    """Typed async event bus with pub/sub and async iteration."""

    def __init__(self):
        self._subscribers: dict[type, list[EventHandler]] = defaultdict(list)
        self._queues: dict[type, list[asyncio.Queue]] = defaultdict(list)
        self._tasks: set[asyncio.Future] = set()

    def on(self, event_type: type[T]):
        """Decorator to subscribe a handler to an event type."""
        def decorator(fn: Callable[[T], HandlerResult]) -> Callable[[T], HandlerResult]:
            self._subscribers[event_type].append(cast(EventHandler, fn))
            return fn
        return decorator

    def subscribe(self, event_type: type[T], handler: Callable[[T], HandlerResult]) -> None:
        """Imperatively subscribe a handler to an event type."""
        self._subscribers[event_type].append(cast(EventHandler, handler))

    def unsubscribe(self, event_type: type[T], handler: Callable[[T], HandlerResult]) -> None:
        """Remove a handler subscription."""
        subs = self._subscribers.get(event_type, [])
        stored_handler = cast(EventHandler, handler)
        if stored_handler in subs:
            subs.remove(stored_handler)

    async def emit(self, event: Event) -> None:
        """Emit an event to all subscribers and queues."""
        for fn in list(self._subscribers.get(type(event), [])):
            try:
                result = fn(event)
            except Exception as exc:
                self._log_subscriber_exception(exc)
                continue

            if inspect.isawaitable(result):
                task = asyncio.ensure_future(result)
                self._tasks.add(task)
                task.add_done_callback(self._task_done)

        for q in list(self._queues.get(type(event), [])):
            await q.put(event)

    def _task_done(self, task: asyncio.Future) -> None:
        """Handle completed subscriber tasks: log exceptions, then discard."""
        self._tasks.discard(task)
        if not task.cancelled():
            exc = task.exception()
            if exc:
                self._log_subscriber_exception(exc)

    def _log_subscriber_exception(self, exc: BaseException) -> None:
        """Log subscriber exceptions without aborting event fanout."""
        logger.error("Event subscriber raised %s", type(exc).__name__)

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
