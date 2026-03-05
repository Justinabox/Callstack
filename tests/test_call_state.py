"""Tests for the call state machine."""

import pytest
from callstack.events.types import CallState
from callstack.errors import InvalidStateTransition
from callstack.voice.state import CallStateMachine


@pytest.fixture
def fsm():
    return CallStateMachine()


async def test_initial_state(fsm):
    assert fsm.state == CallState.IDLE


async def test_valid_outbound_flow(fsm):
    """IDLE -> DIALING -> ACTIVE -> ENDED -> IDLE"""
    await fsm.transition(CallState.DIALING)
    assert fsm.state == CallState.DIALING

    await fsm.transition(CallState.ACTIVE)
    assert fsm.state == CallState.ACTIVE

    await fsm.transition(CallState.ENDED)
    assert fsm.state == CallState.ENDED

    await fsm.transition(CallState.IDLE)
    assert fsm.state == CallState.IDLE


async def test_valid_inbound_flow(fsm):
    """IDLE -> RINGING -> ACTIVE -> ENDED -> IDLE"""
    await fsm.transition(CallState.RINGING)
    assert fsm.state == CallState.RINGING

    await fsm.transition(CallState.ACTIVE)
    assert fsm.state == CallState.ACTIVE

    await fsm.transition(CallState.ENDED)
    assert fsm.state == CallState.ENDED

    await fsm.transition(CallState.IDLE)
    assert fsm.state == CallState.IDLE


async def test_hold_and_resume(fsm):
    """ACTIVE -> HELD -> ACTIVE -> ENDED"""
    await fsm.transition(CallState.DIALING)
    await fsm.transition(CallState.ACTIVE)

    await fsm.transition(CallState.HELD)
    assert fsm.state == CallState.HELD

    await fsm.transition(CallState.ACTIVE)
    assert fsm.state == CallState.ACTIVE

    await fsm.transition(CallState.ENDED)
    assert fsm.state == CallState.ENDED


async def test_reject_during_ringing(fsm):
    """IDLE -> RINGING -> ENDED (caller rejects)"""
    await fsm.transition(CallState.RINGING)
    await fsm.transition(CallState.ENDED)
    assert fsm.state == CallState.ENDED


async def test_dial_failure(fsm):
    """IDLE -> DIALING -> ENDED (dial fails)"""
    await fsm.transition(CallState.DIALING)
    await fsm.transition(CallState.ENDED)
    assert fsm.state == CallState.ENDED


async def test_invalid_idle_to_active(fsm):
    with pytest.raises(InvalidStateTransition):
        await fsm.transition(CallState.ACTIVE)


async def test_invalid_idle_to_ended(fsm):
    with pytest.raises(InvalidStateTransition):
        await fsm.transition(CallState.ENDED)


async def test_invalid_active_to_dialing(fsm):
    await fsm.transition(CallState.DIALING)
    await fsm.transition(CallState.ACTIVE)
    with pytest.raises(InvalidStateTransition):
        await fsm.transition(CallState.DIALING)


async def test_invalid_ringing_to_held(fsm):
    await fsm.transition(CallState.RINGING)
    with pytest.raises(InvalidStateTransition):
        await fsm.transition(CallState.HELD)


async def test_listener_called_on_transition(fsm):
    transitions = []

    async def listener(old, new):
        transitions.append((old, new))

    fsm.on_transition(listener)

    await fsm.transition(CallState.DIALING)
    await fsm.transition(CallState.ACTIVE)

    assert transitions == [
        (CallState.IDLE, CallState.DIALING),
        (CallState.DIALING, CallState.ACTIVE),
    ]


async def test_multiple_listeners(fsm):
    results = {"a": [], "b": []}

    async def listener_a(old, new):
        results["a"].append(new)

    async def listener_b(old, new):
        results["b"].append(new)

    fsm.on_transition(listener_a)
    fsm.on_transition(listener_b)

    await fsm.transition(CallState.RINGING)

    assert results["a"] == [CallState.RINGING]
    assert results["b"] == [CallState.RINGING]


async def test_reset(fsm):
    await fsm.transition(CallState.DIALING)
    await fsm.transition(CallState.ACTIVE)

    await fsm.reset()
    assert fsm.state == CallState.IDLE


async def test_reset_from_idle_is_noop(fsm):
    """Reset when already IDLE should not fire listeners."""
    transitions = []

    async def listener(old, new):
        transitions.append((old, new))

    fsm.on_transition(listener)
    await fsm.reset()

    assert transitions == []
    assert fsm.state == CallState.IDLE
