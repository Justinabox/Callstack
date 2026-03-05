"""DTMF digit collector with buffered input, timeout, and terminator support."""

import asyncio
import logging
from typing import Optional

from callstack.events.bus import EventBus
from callstack.events.types import DTMFEvent

logger = logging.getLogger("callstack.voice.dtmf")


class DTMFCollector:
    """Collects DTMF digits from the event bus with configurable behavior.

    Supports:
    - Maximum digit count (stops collecting after N digits)
    - Timeout (inter-digit and overall)
    - Terminator character (e.g. '#' ends collection early)
    - Single-digit collection for menu input
    - Interruptible collection (for play-and-collect patterns)
    """

    def __init__(
        self,
        bus: EventBus,
        max_digits: int = 10,
        timeout: float = 10.0,
        terminator: str = "#",
        inter_digit_timeout: Optional[float] = None,
    ):
        self._bus = bus
        self.max_digits = max_digits
        self.timeout = timeout
        self.terminator = terminator
        self.inter_digit_timeout = inter_digit_timeout

    async def collect(
        self,
        max_digits: Optional[int] = None,
        timeout: Optional[float] = None,
        inter_digit_timeout: Optional[float] = None,
    ) -> str:
        """Collect DTMF digits until max_digits, terminator, or timeout.

        Args:
            max_digits: Override instance max_digits for this collection.
            timeout: Override instance overall timeout for this collection.
            inter_digit_timeout: If set, resets the deadline after each digit.
                Useful for variable-length input where the user pauses to finish.

        Returns:
            Collected digits as a string (terminator excluded).
        """
        max_d = max_digits if max_digits is not None else self.max_digits
        overall_timeout = timeout if timeout is not None else self.timeout
        idt = inter_digit_timeout if inter_digit_timeout is not None else self.inter_digit_timeout
        digits: list[str] = []

        async with self._bus.stream(DTMFEvent) as events:
            now = asyncio.get_running_loop().time
            overall_deadline = now() + overall_timeout
            deadline = overall_deadline

            while len(digits) < max_d:
                remaining = deadline - now()
                if remaining <= 0:
                    logger.debug("DTMF collect timed out after %d digits", len(digits))
                    break

                event = await events.next(timeout=remaining)
                if event is None:
                    break  # timeout

                digit = event.digit
                logger.debug("DTMF received: %s", digit)

                if digit == self.terminator:
                    logger.debug("Terminator '%s' received", self.terminator)
                    break

                digits.append(digit)

                # Reset deadline on inter-digit timeout, capped by overall deadline
                if idt is not None:
                    deadline = min(overall_deadline, now() + idt)

        result = "".join(digits)
        logger.debug("DTMF collected: '%s'", result)
        return result

    async def collect_one(self, timeout: Optional[float] = None) -> Optional[str]:
        """Collect a single DTMF digit. Returns None on timeout.

        This is optimized for the interrupt pattern in play_and_collect:
        wait for one keypress to know if the user wants to interact.
        """
        result = await self.collect(max_digits=1, timeout=timeout or self.timeout)
        return result if result else None

    async def collect_from_stream(
        self,
        events,
        max_digits: Optional[int] = None,
        timeout: Optional[float] = None,
        inter_digit_timeout: Optional[float] = None,
    ) -> str:
        """Collect digits from an already-open EventStream.

        Use this to avoid closing and reopening streams between
        collection calls (which would lose events in the gap).
        """
        max_d = max_digits if max_digits is not None else self.max_digits
        overall_timeout = timeout if timeout is not None else self.timeout
        idt = inter_digit_timeout if inter_digit_timeout is not None else self.inter_digit_timeout
        digits: list[str] = []

        now = asyncio.get_running_loop().time
        overall_deadline = now() + overall_timeout
        deadline = overall_deadline

        while len(digits) < max_d:
            remaining = deadline - now()
            if remaining <= 0:
                break

            event = await events.next(timeout=remaining)
            if event is None:
                break

            digit = event.digit
            logger.debug("DTMF received: %s", digit)

            if digit == self.terminator:
                logger.debug("Terminator '%s' received", self.terminator)
                break

            digits.append(digit)

            # Reset deadline on inter-digit timeout, capped by overall deadline
            if idt is not None:
                deadline = min(overall_deadline, now() + idt)

        result = "".join(digits)
        logger.debug("DTMF collected: '%s'", result)
        return result

    async def collect_one_from_stream(
        self, events, timeout: Optional[float] = None
    ) -> Optional[str]:
        """Collect a single digit from an already-open EventStream."""
        result = await self.collect_from_stream(
            events, max_digits=1, timeout=timeout or self.timeout
        )
        return result if result else None
