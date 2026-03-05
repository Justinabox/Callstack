"""Tests for IVRMenu and IVRFlow."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from callstack.voice.ivr import IVRMenu, IVRFlow


def _make_session(play_and_collect_returns=None, is_active=True):
    """Create a mock CallSession for IVR testing."""
    session = AsyncMock()
    session.is_active = is_active

    if isinstance(play_and_collect_returns, list):
        session.play_and_collect = AsyncMock(side_effect=play_and_collect_returns)
    else:
        session.play_and_collect = AsyncMock(return_value=play_and_collect_returns or "")

    session.play = AsyncMock()
    return session


async def test_menu_valid_choice():
    handler = AsyncMock()
    menu = IVRMenu(prompt="menu.wav")
    menu.option("1", "Sales", handler)

    session = _make_session(play_and_collect_returns="1")
    result = await menu.run(session)

    assert result == "1"
    handler.assert_awaited_once_with(session)


async def test_menu_routes_to_correct_handler():
    sales = AsyncMock()
    support = AsyncMock()

    menu = IVRMenu(prompt="menu.wav")
    menu.option("1", "Sales", sales)
    menu.option("2", "Support", support)

    session = _make_session(play_and_collect_returns="2")
    result = await menu.run(session)

    assert result == "2"
    sales.assert_not_awaited()
    support.assert_awaited_once_with(session)


async def test_menu_invalid_then_valid():
    handler = AsyncMock()
    menu = IVRMenu(prompt="menu.wav")
    menu.option("1", "Sales", handler)

    session = _make_session(play_and_collect_returns=["9", "1"])
    result = await menu.run(session, retries=3, invalid_prompt="invalid.wav")

    assert result == "1"
    handler.assert_awaited_once()
    session.play.assert_awaited_once_with("invalid.wav")


async def test_menu_exhausted_retries():
    handler = AsyncMock()
    menu = IVRMenu(prompt="menu.wav")
    menu.option("1", "Sales", handler)

    session = _make_session(play_and_collect_returns=["9", "8", "7"])
    result = await menu.run(session, retries=3, invalid_prompt="invalid.wav")

    assert result is None
    handler.assert_not_awaited()
    # invalid_prompt played on first 2 attempts, not the last
    assert session.play.await_count == 2


async def test_menu_timeout_no_input():
    handler = AsyncMock()
    menu = IVRMenu(prompt="menu.wav")
    menu.option("1", "Sales", handler)

    session = _make_session(play_and_collect_returns=["", "", ""])
    result = await menu.run(session, retries=3)

    assert result is None
    handler.assert_not_awaited()


async def test_menu_timeout_with_timeout_prompt():
    handler = AsyncMock()
    menu = IVRMenu(prompt="menu.wav")
    menu.option("1", "Sales", handler)

    session = _make_session(play_and_collect_returns=["", "1"])
    result = await menu.run(session, retries=3, timeout_prompt="timeout.wav")

    assert result == "1"
    session.play.assert_awaited_once_with("timeout.wav")


async def test_menu_goodbye_prompt_on_exhaust():
    menu = IVRMenu(prompt="menu.wav")
    menu.option("1", "Sales", AsyncMock())

    session = _make_session(play_and_collect_returns=["9", "9"])
    result = await menu.run(session, retries=2, goodbye_prompt="goodbye.wav")

    assert result is None
    session.play.assert_awaited_once_with("goodbye.wav")


async def test_menu_inactive_session():
    handler = AsyncMock()
    menu = IVRMenu(prompt="menu.wav")
    menu.option("1", "Sales", handler)

    session = _make_session(is_active=False)
    result = await menu.run(session)

    assert result is None
    handler.assert_not_awaited()
    session.play_and_collect.assert_not_awaited()


async def test_menu_passes_interrupt_and_timeout():
    menu = IVRMenu(prompt="menu.wav", timeout=15.0, interrupt=False)
    menu.option("1", "Sales", AsyncMock())

    session = _make_session(play_and_collect_returns="1")
    await menu.run(session)

    session.play_and_collect.assert_awaited_once_with(
        "menu.wav", max_digits=1, timeout=15.0, interrupt=False
    )


async def test_menu_fluent_chaining():
    menu = IVRMenu(prompt="menu.wav")
    result = menu.option("1", "A", AsyncMock()).option("2", "B", AsyncMock())
    assert result is menu


async def test_menu_valid_digits():
    menu = IVRMenu(prompt="menu.wav")
    menu.option("1", "A", AsyncMock())
    menu.option("2", "B", AsyncMock())
    menu.option("0", "C", AsyncMock())
    assert menu.valid_digits == {"1", "2", "0"}


# -- IVRFlow --

async def test_flow_goto():
    handler = AsyncMock()
    menu = IVRMenu(prompt="sub.wav")
    menu.option("1", "Action", handler)

    flow = IVRFlow()
    flow.add("submenu", menu)

    session = _make_session(play_and_collect_returns="1")
    result = await flow.goto("submenu", session)

    assert result == "1"
    handler.assert_awaited_once()


async def test_flow_unknown_menu():
    flow = IVRFlow()
    session = _make_session()

    with pytest.raises(KeyError):
        await flow.goto("nonexistent", session)


async def test_flow_fluent_chaining():
    flow = IVRFlow()
    result = flow.add("a", IVRMenu(prompt="a.wav")).add("b", IVRMenu(prompt="b.wav"))
    assert result is flow
