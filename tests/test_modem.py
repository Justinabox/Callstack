"""Tests for the top-level Modem orchestrator."""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from callstack.config import ModemConfig
from callstack.events.bus import EventBus
from callstack.events.types import (
    CallState,
    CallStateEvent,
    ModemDisconnectedEvent,
    ModemReconnectedEvent,
    RingEvent,
    CallerIDEvent,
)
from callstack.errors import TransportError
from callstack.modem import Modem
from callstack.transport.mock import MockTransport


class MockModem(Modem):
    """Modem subclass that uses MockTransports for testing."""

    def __init__(self, config=None):
        # Initialize base config
        self.config = config or ModemConfig()

        # Event bus
        self.bus = EventBus()

        # Use mock transports instead of serial
        self._at_transport = MockTransport()
        self._audio_transport = MockTransport()

        # Protocol layer
        from callstack.protocol.urc import URCDispatcher
        from callstack.protocol.executor import ATCommandExecutor
        from callstack.voice.audio import AudioPipeline
        from callstack.voice.service import CallService
        from callstack.sms.service import SMSService
        from callstack.sms.store import SMSStore
        from callstack.network import NetworkService

        self._urc = URCDispatcher(self.bus)
        self._executor = ATCommandExecutor(self._at_transport, self._urc)

        self._audio = AudioPipeline(self._audio_transport, self.bus)
        self.call = CallService(self._executor, self._audio, self.bus)
        self.sms = SMSService(self._executor, self.bus, SMSStore())
        self.network = NetworkService(self._executor, self.bus)

        self._reconnect_task = None
        self._reconnect_lock = asyncio.Lock()
        self._connected = False
        self._shutdown = asyncio.Event()
        self._call_handlers = []
        self._tasks = set()


def _feed_init_responses(transport: MockTransport):
    """Feed the standard modem initialization AT command responses."""
    # ATE0
    transport.feed("OK")
    # AT+CLIP=1
    transport.feed("OK")
    # AT+CVHU=0
    transport.feed("OK")
    # AT+COLP=1
    transport.feed("OK")
    # SMS init: AT+CMGF=1, AT+CSCS="GSM", AT+CNMI=..., AT+CSMP=...
    transport.feed("OK")
    transport.feed("OK")
    transport.feed("OK")
    transport.feed("OK")


class TestModemInit:
    async def test_context_manager_opens_and_closes(self):
        modem = MockModem()
        _feed_init_responses(modem._at_transport)

        async with modem:
            assert modem._connected is True
            assert modem._executor._reader_active

        assert modem._connected is False

    async def test_initialization_sends_at_commands(self):
        modem = MockModem()
        _feed_init_responses(modem._at_transport)

        async with modem:
            written = modem._at_transport.all_written
            # Check key init commands were sent
            assert any("ATE0" in w for w in written)
            assert any("AT+CLIP=1" in w for w in written)
            assert any("AT+CVHU=0" in w for w in written)
            assert any("AT+COLP=1" in w for w in written)
            # SMS init
            assert any("AT+CMGF=1" in w for w in written)

    async def test_close_is_idempotent(self):
        modem = MockModem()
        _feed_init_responses(modem._at_transport)

        async with modem:
            pass

        # Calling close again should not raise
        await modem.close()


class TestModemOnCall:
    async def test_on_call_decorator_registers_handler(self):
        modem = MockModem()
        _feed_init_responses(modem._at_transport)

        async with modem:
            @modem.on_call
            async def handler(session):
                pass

            assert len(modem._call_handlers) == 1

    async def test_on_call_returns_handler(self):
        modem = MockModem()

        @modem.on_call
        async def handler(session):
            pass

        assert handler.__name__ == "handler"

    async def test_multiple_handlers(self):
        modem = MockModem()

        @modem.on_call
        async def handler1(session):
            pass

        @modem.on_call
        async def handler2(session):
            pass

        assert len(modem._call_handlers) == 2


class TestModemRunForever:
    async def test_shutdown_stops_run_forever(self):
        modem = MockModem()
        _feed_init_responses(modem._at_transport)

        async with modem:
            # Schedule shutdown after a short delay
            async def stop():
                await asyncio.sleep(0.05)
                modem.shutdown()

            asyncio.create_task(stop())
            await asyncio.wait_for(modem.run_forever(), timeout=1.0)

    async def test_shutdown_method(self):
        modem = MockModem()
        assert not modem._shutdown.is_set()
        modem.shutdown()
        assert modem._shutdown.is_set()


class TestModemExecute:
    async def test_raw_at_command(self):
        modem = MockModem()
        _feed_init_responses(modem._at_transport)

        async with modem:
            modem._at_transport.feed("+CSQ: 20,0", "OK")
            resp = await modem.execute("AT+CSQ")

            assert resp.success is True
            assert any("+CSQ:" in line for line in resp.lines)


class TestModemURCReader:
    async def test_urc_dispatches_events(self):
        modem = MockModem()
        _feed_init_responses(modem._at_transport)

        received = []
        modem.bus.subscribe(RingEvent, lambda e: received.append(e))

        async with modem:
            # Feed a RING URC
            modem._at_transport.feed("RING")
            await asyncio.sleep(0.05)

            assert len(received) >= 1

    async def test_transport_error_emits_disconnected(self):
        modem = MockModem(ModemConfig(auto_reconnect=False))
        _feed_init_responses(modem._at_transport)

        disconnected = []
        modem.bus.subscribe(ModemDisconnectedEvent, lambda e: disconnected.append(e))

        async with modem:
            # Override readline to raise TransportError
            original_readline = modem._at_transport.readline

            async def failing_readline():
                raise TransportError("USB disconnected")

            modem._at_transport.readline = failing_readline
            await asyncio.sleep(0.1)

            assert len(disconnected) >= 1
            assert "USB disconnected" in disconnected[0].reason


class TestModemAutoReconnect:
    async def test_reconnect_on_transport_error(self):
        modem = MockModem(ModemConfig(auto_reconnect=True, reconnect_interval=0.05))
        _feed_init_responses(modem._at_transport)

        reconnected = []
        modem.bus.subscribe(ModemReconnectedEvent, lambda e: reconnected.append(e))

        async with modem:
            # Make readline fail once to trigger disconnect
            fail_count = 0

            original_readline = modem._at_transport.readline

            async def failing_then_ok():
                nonlocal fail_count
                fail_count += 1
                if fail_count <= 1:
                    raise TransportError("USB disconnected")
                return await original_readline()

            modem._at_transport.readline = failing_then_ok

            # Feed responses for re-initialization
            _feed_init_responses(modem._at_transport)

            # Wait for reconnect cycle
            await asyncio.sleep(0.3)

            # Feed an idle response so the URC reader doesn't block
            modem._at_transport.feed("RING")

            assert modem._connected is True
            assert len(reconnected) >= 1


class TestRegistrationInfo:
    def test_registered_home(self):
        from callstack.network import RegistrationInfo
        info = RegistrationInfo(status=1, mode=0)
        assert info.registered is True
        assert info.roaming is False

    def test_roaming(self):
        from callstack.network import RegistrationInfo
        info = RegistrationInfo(status=5, mode=0)
        assert info.registered is True
        assert info.roaming is True

    def test_not_registered(self):
        from callstack.network import RegistrationInfo
        info = RegistrationInfo(status=0, mode=0)
        assert info.registered is False
        assert info.roaming is False

    def test_denied(self):
        from callstack.network import RegistrationInfo
        info = RegistrationInfo(status=3, mode=0)
        assert info.registered is False
        assert "denied" in info.description
