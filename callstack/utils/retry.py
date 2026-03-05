"""Retry/backoff decorator for flaky AT commands."""

import asyncio
import functools
import logging
from typing import Callable, TypeVar

from callstack.errors import ATTimeoutError, ATCommandError, TransportError

logger = logging.getLogger("callstack.utils.retry")

F = TypeVar("F", bound=Callable)

# Exceptions that are worth retrying by default
RETRYABLE = (ATTimeoutError, ATCommandError, TransportError, OSError)


def retry(
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 10.0,
    backoff_factor: float = 2.0,
    retryable: tuple[type[Exception], ...] = RETRYABLE,
) -> Callable[[F], F]:
    """Decorator that retries an async function with exponential backoff.

    Args:
        max_attempts: Total attempts (1 = no retry).
        base_delay: Initial delay between retries in seconds.
        max_delay: Cap on the delay between retries.
        backoff_factor: Multiplier applied to delay after each failure.
        retryable: Exception types that trigger a retry.

    Usage:
        @retry(max_attempts=3)
        async def send_command():
            return await executor.execute("AT+CSQ")
    """
    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            delay = base_delay
            last_exc: Exception | None = None

            for attempt in range(1, max_attempts + 1):
                try:
                    return await fn(*args, **kwargs)
                except retryable as exc:
                    last_exc = exc
                    if attempt == max_attempts:
                        logger.error(
                            "%s failed after %d attempts: %s",
                            fn.__qualname__, max_attempts, exc,
                        )
                        raise
                    logger.warning(
                        "%s attempt %d/%d failed (%s), retrying in %.1fs",
                        fn.__qualname__, attempt, max_attempts, exc, delay,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * backoff_factor, max_delay)

            raise last_exc  # unreachable, but satisfies type checkers

        return wrapper  # type: ignore[return-value]
    return decorator
