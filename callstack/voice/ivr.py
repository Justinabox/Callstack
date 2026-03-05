"""Declarative IVR menu builder for interactive voice response flows."""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable, Awaitable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from callstack.voice.service import CallSession

logger = logging.getLogger("callstack.voice.ivr")


@dataclass
class MenuOption:
    """A single option in an IVR menu."""
    digit: str
    description: str
    handler: Callable[["CallSession"], Awaitable[None]]


class IVRMenu:
    """Declarative IVR menu builder.

    Plays a prompt, collects a single DTMF digit, and routes to the
    matching handler. Supports retries and an invalid-input prompt.

    Usage:
        menu = IVRMenu(prompt="audio/main_menu.wav")
        menu.option("1", "Sales", handle_sales)
        menu.option("2", "Support", handle_support)
        menu.option("0", "Operator", handle_operator)

        @modem.on_call
        async def on_call(session):
            await menu.run(session, retries=3, invalid_prompt="audio/invalid.wav")
    """

    def __init__(self, prompt: str, timeout: float = 10.0, interrupt: bool = True):
        """
        Args:
            prompt: Path to the WAV file to play as the menu prompt.
            timeout: Seconds to wait for DTMF input after the prompt.
            interrupt: If True, stop playing the prompt on first keypress.
        """
        self.prompt = prompt
        self.timeout = timeout
        self.interrupt = interrupt
        self._options: dict[str, MenuOption] = {}

    def option(
        self,
        digit: str,
        description: str,
        handler: Callable[["CallSession"], Awaitable[None]],
    ) -> "IVRMenu":
        """Register a menu option.

        Args:
            digit: The DTMF digit that selects this option (e.g. "1", "0", "*").
            description: Human-readable description (for logging/debugging).
            handler: Async callable receiving the CallSession.

        Returns:
            self, for fluent chaining.
        """
        self._options[digit] = MenuOption(digit=digit, description=description, handler=handler)
        return self

    @property
    def valid_digits(self) -> set[str]:
        """The set of digits that have registered handlers."""
        return set(self._options.keys())

    async def run(
        self,
        session: "CallSession",
        retries: int = 3,
        invalid_prompt: Optional[str] = None,
        timeout_prompt: Optional[str] = None,
        goodbye_prompt: Optional[str] = None,
    ) -> Optional[str]:
        """Execute the IVR menu loop.

        Plays the prompt, collects input, and dispatches to the handler.
        On invalid input or timeout, replays the prompt up to `retries` times.

        Args:
            session: The active CallSession.
            retries: Number of attempts before giving up.
            invalid_prompt: WAV to play on unrecognized input (optional).
            timeout_prompt: WAV to play when no input received (optional).
                Falls back to invalid_prompt if not set.
            goodbye_prompt: WAV to play after exhausting retries (optional).

        Returns:
            The digit that was selected, or None if retries exhausted.
        """
        for attempt in range(retries):
            if not session.is_active:
                logger.debug("Session no longer active, aborting menu")
                return None

            logger.debug("Menu attempt %d/%d", attempt + 1, retries)

            choice = await session.play_and_collect(
                self.prompt,
                max_digits=1,
                timeout=self.timeout,
                interrupt=self.interrupt,
            )

            if choice in self._options:
                opt = self._options[choice]
                logger.info("Menu selection: '%s' (%s)", choice, opt.description)
                await opt.handler(session)
                return choice

            # Handle invalid/no input
            is_last = attempt >= retries - 1

            if not choice:
                logger.debug("No DTMF input received (timeout)")
                if not is_last and (timeout_prompt or invalid_prompt):
                    await session.play(timeout_prompt or invalid_prompt)
            else:
                logger.debug("Invalid menu choice: '%s'", choice)
                if not is_last and invalid_prompt:
                    await session.play(invalid_prompt)

        # Exhausted retries
        logger.info("Menu retries exhausted (%d attempts)", retries)
        if goodbye_prompt and session.is_active:
            await session.play(goodbye_prompt)

        return None


class IVRFlow:
    """Chain multiple IVR menus into a multi-level flow.

    Usage:
        flow = IVRFlow()
        flow.add("main", main_menu)
        flow.add("sales", sales_menu)

        # In a handler:
        async def handle_sales(session):
            await flow.goto("sales", session)
    """

    def __init__(self):
        self._menus: dict[str, IVRMenu] = {}

    def add(self, name: str, menu: IVRMenu) -> "IVRFlow":
        """Register a named menu in the flow."""
        self._menus[name] = menu
        return self

    async def goto(
        self,
        name: str,
        session: "CallSession",
        retries: int = 3,
        invalid_prompt: Optional[str] = None,
        **kwargs,
    ) -> Optional[str]:
        """Run a named menu. Raises KeyError if name not found."""
        menu = self._menus[name]
        return await menu.run(session, retries=retries, invalid_prompt=invalid_prompt, **kwargs)
