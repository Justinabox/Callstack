"""Modem: top-level orchestrator and application entry point."""

import asyncio
import logging
from contextlib import suppress
from typing import Awaitable, Callable, Optional

from callstack.config import ModemConfig
from callstack.errors import TransportError, SIMPINRequired, SIMPUKRequired, SIMUnlockError
from callstack.events.bus import EventBus
from callstack.events.types import (
    CallState,
    ModemDisconnectedEvent,
    ModemReconnectedEvent,
    RingEvent,
)
from callstack.network import NetworkService
from callstack.protocol.commands import ATCommand
from callstack.protocol.parser import ATResponseParser
from callstack.protocol.executor import ATCommandExecutor
from callstack.protocol.urc import URCDispatcher
from callstack.sms.service import SMSService
from callstack.sms.store import SMSStore
from callstack.transport.serial import SerialTransport
from callstack.ussd import USSDService
from callstack.utils.logger import setup_logging
from callstack.voice.audio import AudioPipeline
from callstack.voice.service import CallService, CallSession

logger = logging.getLogger("callstack.modem")


class Modem:
    """Top-level entry point. Manages all subsystems.

    Usage:
        async with Modem(ModemConfig()) as modem:
            @modem.on_call
            async def handle_call(session: CallSession):
                await session.play("audio/greeting.wav")
                choice = await session.collect_dtmf(max_digits=1)
                ...

            await modem.sms.send("+1234567890", "Hello from Callstack!")
            await modem.run_forever()
    """

    def __init__(self, config: ModemConfig | None = None):
        self.config = config or ModemConfig()

        # Logging
        setup_logging(self.config.log_level)

        # Event bus (shared across all subsystems)
        self.bus = EventBus()

        # Transports
        self._at_transport = SerialTransport(self.config.at_port, self.config.baudrate)
        self._audio_transport = SerialTransport(self.config.audio_port, self.config.baudrate)

        # Protocol layer
        self._urc = URCDispatcher(self.bus)
        self._executor = ATCommandExecutor(self._at_transport, self._urc)

        # Services
        self._audio = AudioPipeline(self._audio_transport, self.bus)
        self.call = CallService(
            self._executor,
            self._audio,
            self.bus,
            command_timeout=self.config.command_timeout,
        )
        self.sms = SMSService(
            self._executor,
            self.bus,
            SMSStore(self.config.sms_db_path) if self.config.sms_db_path else None,
            command_timeout=self.config.command_timeout,
            sms_prompt_timeout=self.config.sms_prompt_timeout,
            sms_submit_timeout=self.config.sms_submit_timeout,
        )
        self.network = NetworkService(
            self._executor, self.bus, command_timeout=self.config.command_timeout
        )
        self.ussd = USSDService(
            self._executor, self.bus, command_timeout=self.config.command_timeout
        )

        # Internal state
        self._reconnect_task: Optional[asyncio.Task] = None
        self._reconnect_lock = asyncio.Lock()
        self._connected = False
        self._shutdown = asyncio.Event()
        self._call_handlers: list[Callable[[CallSession], Awaitable[None]]] = []
        self._tasks: set[asyncio.Task] = set()

        # Wire incoming call routing (subscribe once, not per-connection)
        self.bus.subscribe(RingEvent, self._on_ring)

    @property
    def connected(self) -> bool:
        """Whether the modem has completed startup and is currently connected."""
        return self._connected

    # -- Context manager --

    async def __aenter__(self) -> "Modem":
        try:
            await self._connect()
        except BaseException:
            try:
                await self._cleanup_partial_connect()
            except Exception as exc:
                logger.debug("Startup cleanup failed: %s", exc)
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def _connect(self) -> None:
        """Open transport, initialize modem, start background tasks."""
        logger.info("Connecting to modem on %s", self.config.at_port)
        await self._at_transport.open()
        await self._initialize_modem()
        await self.sms.initialize()
        self._connected = True

        # Start the executor's reader loop (single reader for all transport I/O)
        await self._executor.start_reader()
        self._executor.on_reader_done(self._reader_done_callback)

        logger.info("Modem ready")

    async def _cleanup_partial_connect(self) -> None:
        """Best-effort cleanup after startup fails before __aexit__ can run."""
        self._connected = False
        try:
            await self._executor.stop_reader()
        except Exception as exc:
            logger.debug("Executor stop during startup cleanup: %s", exc)
        try:
            await self._audio.stop()
        except Exception as exc:
            logger.debug("Audio stop during startup cleanup: %s", exc)
        try:
            await self.sms._store.close()
        except Exception as exc:
            logger.debug("SMS store close during startup cleanup: %s", exc)
        try:
            await self._audio_transport.close()
        except Exception as exc:
            logger.debug("Audio transport close during startup cleanup: %s", exc)
        try:
            await self._at_transport.close()
        except Exception as exc:
            logger.debug("AT transport close during startup cleanup: %s", exc)

    async def close(self) -> None:
        """Shut down all subsystems and close transports."""
        logger.info("Shutting down modem")
        self._shutdown.set()
        self._connected = False

        # Stop executor reader and cancel background tasks
        await self._executor.stop_reader()

        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reconnect_task

        # Stop services
        self.bus.unsubscribe(RingEvent, self._on_ring)
        self.call.close()
        try:
            await self._audio.stop()
        except (TransportError, OSError) as exc:
            logger.debug("Audio stop during shutdown: %s", exc)
        try:
            await self.sms._store.close()
        except Exception as exc:
            logger.debug("SMS store close during shutdown: %s", exc)

        # Close transports
        try:
            await self._audio_transport.close()
        except (TransportError, OSError) as exc:
            logger.debug("Audio transport close during shutdown: %s", exc)
        try:
            await self._at_transport.close()
        except (TransportError, OSError) as exc:
            logger.debug("AT transport close during shutdown: %s", exc)

        logger.info("Modem shutdown complete")

    async def _initialize_modem(self) -> None:
        """Send initialization AT commands."""
        # Disable echo
        await self._executor.execute(ATCommand.ECHO_OFF, timeout=self.config.command_timeout)

        # Check and handle SIM PIN
        await self._check_sim_pin()

        # Enable caller ID presentation
        await self._executor.execute(ATCommand.CLIP_ENABLE, timeout=self.config.command_timeout)
        # Disconnect control (ATH always works)
        await self._executor.execute(ATCommand.CVHU, timeout=self.config.command_timeout)
        # Connected line ID
        await self._executor.execute(ATCommand.COLP_ENABLE, timeout=self.config.command_timeout)

        logger.debug("Modem initialization complete")

    async def _check_sim_pin(self) -> None:
        """Check SIM PIN status and unlock if needed."""
        resp = await self._executor.execute(
            ATCommand.CPIN_QUERY, expect=["OK"], timeout=self.config.command_timeout
        )
        if not resp.success:
            logger.warning("Could not query SIM PIN status: %s", resp.lines)
            return

        status = None
        for line in resp.data_lines:
            status = ATResponseParser.parse_cpin(line)
            if status:
                break

        if status is None:
            logger.warning("Could not parse SIM PIN status from: %s", resp.lines)
            return

        logger.info("SIM status: %s", status)

        if status == "READY":
            return

        if status == "SIM PIN":
            if not self.config.sim_pin:
                raise SIMPINRequired("SIM is locked — set sim_pin in ModemConfig to unlock")
            pin_resp = await self._executor.execute(
                ATCommand.cpin_enter(self.config.sim_pin), expect=["OK"], timeout=10.0
            )
            if not pin_resp.success:
                raise SIMUnlockError(f"PIN rejected: {pin_resp.lines}")
            logger.info("SIM unlocked with PIN")

        elif status == "SIM PUK":
            raise SIMPUKRequired(
                "SIM is PUK-locked — use Modem.recover_puk(config, puk, new_pin) to recover"
            )
        else:
            logger.warning("Unhandled SIM status: %s", status)

    @classmethod
    async def recover_puk(
        cls, config: ModemConfig | None, puk: str, new_pin: str
    ) -> None:
        """Recover a PUK-locked SIM without running normal modem startup.

        This opens only the AT transport, verifies the SIM is currently asking
        for a PUK, sends the PUK/new-PIN command, verifies the SIM reports
        READY, and closes the transport. PUK/PIN values are validated before
        the transport is opened so invalid credentials are never written.
        """
        unlock_command = ATCommand.cpin_puk(puk, new_pin)
        modem = cls(config)

        try:
            await modem._at_transport.open()
            echo_resp = await modem._executor.execute(
                ATCommand.ECHO_OFF, expect=["OK"], timeout=5.0
            )
            if not echo_resp.success:
                raise SIMUnlockError("could not prepare modem for PUK recovery")

            status = await modem._query_sim_status_for_recovery()
            if status != "SIM PUK":
                raise SIMUnlockError(
                    f"SIM is not PUK-locked; current status is {status or 'unknown'}"
                )

            unlock_resp = await modem._executor.execute(
                unlock_command,
                expect=["OK"],
                timeout=10.0,
                log_command='AT+CPIN="<PUK>","<new PIN>"',
            )
            if not unlock_resp.success:
                raise SIMUnlockError("PUK recovery command was rejected by modem")

            status = await modem._query_sim_status_for_recovery()
            if status != "READY":
                raise SIMUnlockError(
                    f"PUK recovery did not leave SIM ready; current status is {status or 'unknown'}"
                )

            logger.info("SIM recovered with PUK, new PIN set")
        finally:
            await modem._at_transport.close()

    async def _query_sim_status_for_recovery(self) -> str | None:
        resp = await self._executor.execute(ATCommand.CPIN_QUERY, expect=["OK"], timeout=5.0)
        if not resp.success:
            return None

        for line in resp.data_lines:
            status = ATResponseParser.parse_cpin(line)
            if status:
                return status
        return None

    def _reader_done_callback(self, task: asyncio.Task) -> None:
        """Handle executor reader task completion (usually a transport error)."""
        if task.cancelled() or self._shutdown.is_set():
            return
        exc = task.exception()
        if exc:
            logger.error("Reader task failed: %s", exc)
            self._connected = False
            # Schedule async work from the sync callback
            task = asyncio.create_task(self._handle_reader_failure(exc))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _handle_reader_failure(self, exc: Exception) -> None:
        """Emit disconnect event and optionally start auto-reconnect."""
        await self.bus.emit(ModemDisconnectedEvent(reason=str(exc)))
        if self.config.auto_reconnect:
            async with self._reconnect_lock:
                if self._reconnect_task is not None and not self._reconnect_task.done():
                    logger.debug("Reconnect already in progress, skipping duplicate")
                else:
                    self._reconnect_task = asyncio.create_task(self._auto_reconnect())

    # -- Auto-reconnect --

    async def _auto_reconnect(self) -> None:
        """Attempt to re-establish the modem connection with exponential backoff."""
        delay = self.config.reconnect_interval
        max_delay = 60.0
        attempt = 0

        while not self._shutdown.is_set():
            attempt += 1
            logger.info("Reconnect attempt %d in %.1fs", attempt, delay)
            await asyncio.sleep(delay)

            if self._shutdown.is_set():
                break

            try:
                # Close stale transport
                try:
                    await self._at_transport.close()
                except (TransportError, OSError) as exc:
                    logger.debug("Stale transport close: %s", exc)

                # Re-open
                await self._at_transport.open()
                await self._initialize_modem()
                await self.sms.initialize()
                self._connected = True

                # Restart executor reader (callback auto-attached via persistent registration)
                await self._executor.start_reader()

                logger.info("Reconnected successfully on attempt %d", attempt)
                await self.bus.emit(ModemReconnectedEvent())
                return

            except (TransportError, OSError) as exc:
                logger.warning("Reconnect attempt %d failed: %s", attempt, exc)
                delay = min(delay * 2, max_delay)

            except Exception as exc:
                logger.exception("Unexpected error during reconnect: %s", exc)
                delay = min(delay * 2, max_delay)

    # -- Incoming call routing --

    def on_call(
        self, handler: Callable[[CallSession], Awaitable[None]]
    ) -> Callable[[CallSession], Awaitable[None]]:
        """Decorator: register a handler for incoming calls.

        The handler receives a CallSession after the call is auto-answered.

        Usage:
            @modem.on_call
            async def handle_call(session: CallSession):
                await session.play("audio/greeting.wav")
                await session.hangup()
        """
        self._call_handlers.append(handler)
        return handler

    async def _on_ring(self, event: RingEvent) -> None:
        """Handle RING events by answering and dispatching to registered handlers."""
        if not self._call_handlers:
            return
        if self.call.state != CallState.RINGING:
            return

        try:
            session = await self.call.answer()
            for handler in self._call_handlers:
                task = asyncio.create_task(self._safe_call_handler(handler, session))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        except Exception as exc:
            logger.error("Failed to answer incoming call: %s", exc)

    async def _safe_call_handler(
        self, handler: Callable[[CallSession], Awaitable[None]], session: CallSession
    ) -> None:
        """Run a call handler with error protection."""
        try:
            await handler(session)
        except Exception as exc:
            logger.exception("Call handler error: %s", exc)
            # Ensure we hang up even if the handler crashes
            if session.is_active:
                try:
                    await session.hangup()
                except Exception as exc:
                    logger.debug("Safety hangup failed: %s", exc)

    # -- SIM management --

    async def unlock_puk(self, puk: str, new_pin: str) -> None:
        """Unlock a PUK-locked SIM by providing the PUK and a new PIN."""
        resp = await self._executor.execute(
            ATCommand.cpin_puk(puk, new_pin),
            expect=["OK"],
            timeout=10.0,
            log_command='AT+CPIN="<PUK>","<new PIN>"',
        )
        if not resp.success:
            raise SIMUnlockError(f"PUK unlock failed: {resp.lines}")
        logger.info("SIM unlocked with PUK, new PIN set")

    # -- Raw AT access --

    async def execute(self, command: str, **kwargs):
        """Send a raw AT command. Thin wrapper around the executor."""
        kwargs.setdefault("timeout", self.config.command_timeout)
        return await self._executor.execute(command, **kwargs)

    # -- Run --

    async def run_forever(self) -> None:
        """Block until the modem is shut down.

        Keeps the event loop alive for URC processing and event handlers.
        Call modem.close() or cancel the task to stop.
        """
        logger.info("Modem running — waiting for events")
        await self._shutdown.wait()

    def shutdown(self) -> None:
        """Signal the modem to stop run_forever() and begin shutdown."""
        self._shutdown.set()
