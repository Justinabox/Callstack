"""Tests for SIM PIN management."""

import asyncio
import pytest
from callstack.config import ModemConfig
from callstack.errors import SIMPINRequired, SIMPUKRequired, SIMUnlockError
from callstack.events.bus import EventBus
from callstack.protocol.commands import ATCommand
from callstack.protocol.executor import ATCommandExecutor
from callstack.protocol.urc import URCDispatcher
from callstack.transport.mock import MockTransport


@pytest.fixture
def transport():
    return MockTransport()


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def executor(transport, bus):
    urc = URCDispatcher(bus)
    return ATCommandExecutor(transport, urc)


class TestATCommandPIN:
    def test_cpin_enter_valid(self):
        assert ATCommand.cpin_enter("1234") == 'AT+CPIN="1234"'

    def test_cpin_enter_8_digits(self):
        assert ATCommand.cpin_enter("12345678") == 'AT+CPIN="12345678"'

    def test_cpin_enter_too_short(self):
        with pytest.raises(ValueError):
            ATCommand.cpin_enter("123")

    def test_cpin_enter_too_long(self):
        with pytest.raises(ValueError):
            ATCommand.cpin_enter("123456789")

    def test_cpin_enter_non_numeric(self):
        with pytest.raises(ValueError):
            ATCommand.cpin_enter("abcd")

    def test_cpin_puk_valid(self):
        assert ATCommand.cpin_puk("12345678", "1234") == 'AT+CPIN="12345678","1234"'

    def test_cpin_puk_invalid_puk_length(self):
        with pytest.raises(ValueError):
            ATCommand.cpin_puk("1234", "1234")


class TestCPINParser:
    def test_parse_ready(self):
        from callstack.protocol.parser import ATResponseParser
        assert ATResponseParser.parse_cpin("+CPIN: READY") == "READY"

    def test_parse_sim_pin(self):
        from callstack.protocol.parser import ATResponseParser
        assert ATResponseParser.parse_cpin("+CPIN: SIM PIN") == "SIM PIN"

    def test_parse_sim_puk(self):
        from callstack.protocol.parser import ATResponseParser
        assert ATResponseParser.parse_cpin("+CPIN: SIM PUK") == "SIM PUK"

    def test_parse_invalid(self):
        from callstack.protocol.parser import ATResponseParser
        assert ATResponseParser.parse_cpin("OK") is None


class TestModemSIMPIN:
    async def test_sim_ready_proceeds(self, transport, executor):
        """When SIM is READY, initialization should proceed normally."""
        transport.feed("+CPIN: READY", "OK")
        resp = await executor.execute(ATCommand.CPIN_QUERY, expect=["OK"])
        assert resp.success is True

    async def test_config_has_sim_pin(self):
        config = ModemConfig(sim_pin="1234")
        assert config.sim_pin == "1234"

    async def test_config_default_no_pin(self):
        config = ModemConfig()
        assert config.sim_pin is None
