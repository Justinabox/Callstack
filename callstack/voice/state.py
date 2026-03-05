"""Call state machine with guarded transitions."""

import logging
from typing import Callable, Awaitable

from callstack.events.types import CallState
from callstack.errors import InvalidStateTransition

logger = logging.getLogger("callstack.voice.state")

# Callback type: async fn(old_state, new_state)
StateListener = Callable[[CallState, CallState], Awaitable[None]]


class CallStateMachine:
    """Enforces valid call state transitions.

    Transitions:
        IDLE    -> DIALING, RINGING
        DIALING -> ACTIVE, ENDED
        RINGING -> ACTIVE, ENDED
        ACTIVE  -> HELD, ENDED
        HELD    -> ACTIVE, ENDED
        ENDED   -> IDLE
    """

    TRANSITIONS: dict[CallState, set[CallState]] = {
        CallState.IDLE:    {CallState.DIALING, CallState.RINGING},
        CallState.DIALING: {CallState.ACTIVE, CallState.ENDED},
        CallState.RINGING: {CallState.ACTIVE, CallState.ENDED},
        CallState.ACTIVE:  {CallState.HELD, CallState.ENDED},
        CallState.HELD:    {CallState.ACTIVE, CallState.ENDED},
        CallState.ENDED:   {CallState.IDLE},
    }

    def __init__(self):
        self._state = CallState.IDLE
        self._listeners: list[StateListener] = []

    @property
    def state(self) -> CallState:
        return self._state

    def on_transition(self, listener: StateListener) -> None:
        """Register a callback for state transitions."""
        self._listeners.append(listener)

    async def transition(self, new_state: CallState) -> None:
        """Transition to a new state. Raises InvalidStateTransition if not allowed."""
        allowed = self.TRANSITIONS.get(self._state, set())
        if new_state not in allowed:
            raise InvalidStateTransition(self._state, new_state)

        old = self._state
        self._state = new_state
        logger.info("Call state: %s -> %s", old.name, new_state.name)

        for listener in self._listeners:
            try:
                await listener(old, new_state)
            except Exception as exc:
                logger.exception("State listener error during %s -> %s: %s", old.name, new_state.name, exc)

    async def reset(self) -> None:
        """Force reset to IDLE (for cleanup after errors)."""
        if self._state != CallState.IDLE:
            old = self._state
            self._state = CallState.IDLE
            logger.info("Call state reset: %s -> IDLE", old.name)
            for listener in self._listeners:
                try:
                    await listener(old, CallState.IDLE)
                except Exception as exc:
                    logger.exception("State listener error during reset %s -> IDLE: %s", old.name, exc)
