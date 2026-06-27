"""Tests for the AT command executor."""

import asyncio
import io
import logging
import pytest
from callstack.events.bus import EventBus
from callstack.events.types import CallState, CallStateEvent, RingEvent, DTMFEvent
from callstack.errors import ATTimeoutError, TransportError
from callstack.protocol.executor import ATCommandExecutor, ATResponse
from callstack.protocol.urc import URCDispatcher
from callstack.transport.base import Transport
from callstack.transport.mock import MockTransport


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def transport():
    return MockTransport()


@pytest.fixture
def urc(bus):
    return URCDispatcher(bus)


@pytest.fixture
async def executor(transport, urc):
    ex = ATCommandExecutor(transport, urc)
    await ex.start_reader()
    yield ex
    await ex.stop_reader()


async def test_simple_at_ok(executor, transport):
    """AT -> OK"""
    transport.feed("OK")
    resp = await executor.execute("AT")
    assert resp.success is True
    assert "OK" in resp.lines
    assert transport.last_written == "AT\r\n"


async def test_signal_quality(executor, transport):
    """AT+CSQ -> +CSQ: 20,0 / OK"""
    transport.feed("+CSQ: 20,0", "OK")
    resp = await executor.execute("AT+CSQ")
    assert resp.success is True
    assert "+CSQ: 20,0" in resp.lines
    assert "OK" in resp.lines


async def test_data_lines(executor, transport):
    """data_lines excludes the final result code."""
    transport.feed("+CSQ: 20,0", "OK")
    resp = await executor.execute("AT+CSQ")
    assert resp.data_lines == ["+CSQ: 20,0"]


async def test_padded_ok_final_result_is_normalized_for_control_responses(executor, transport):
    """Trailing modem whitespace on control final codes is not response data."""
    transport.feed("+CSQ: 20,0 ", "OK  ")

    resp = await executor.execute("AT+CSQ")

    assert resp.success is True
    assert resp.lines == ["+CSQ: 20,0", "OK"]
    assert resp.data_lines == ["+CSQ: 20,0"]


async def test_padded_error_final_result_is_normalized_for_control_responses(executor, transport):
    """Trailing modem whitespace on ERROR still completes as a failure."""
    transport.feed("ERROR  ")

    resp = await executor.execute("AT+INVALID")

    assert resp.success is False
    assert resp.lines == ["ERROR"]


def test_data_lines_only_excludes_trailing_final_result_code():
    """Final-code-looking payload lines before the terminator remain data."""
    response = ATResponse(
        success=True,
        lines=[
            "OK",
            "+CME ERROR details from an SMS body",
            "+CMS ERROR details from an SMS body",
            "OK",
        ],
    )
    assert response.data_lines == [
        "OK",
        "+CME ERROR details from an SMS body",
        "+CMS ERROR details from an SMS body",
    ]


def test_data_lines_excludes_dial_terminal_failure_result_code():
    """Dial failure result codes are terminal metadata, not response data."""
    response = ATResponse(success=False, lines=["NO CARRIER"])

    assert response.data_lines == []


async def test_success_result_code_requires_exact_line(executor, transport):
    """A data line containing OK must not terminate the response early."""
    transport.feed("SMS body says OK to proceed", "OK")
    resp = await executor.execute("AT+CMGR=1")
    assert resp.success is True
    assert resp.lines == ["SMS body says OK to proceed", "OK"]
    assert resp.data_lines == ["SMS body says OK to proceed"]


async def test_error_response(executor, transport):
    """AT command returning ERROR."""
    transport.feed("ERROR")
    resp = await executor.execute("AT+INVALID")
    assert resp.success is False


async def test_plain_error_result_code_requires_exact_line(executor, transport):
    """A data line starting with ERROR must not be treated as final ERROR."""
    transport.feed("ERROR appears in this SMS body", "OK")
    resp = await executor.execute("AT+CMGR=1")
    assert resp.success is True
    assert resp.lines == ["ERROR appears in this SMS body", "OK"]
    assert resp.data_lines == ["ERROR appears in this SMS body"]


async def test_cme_error(executor, transport):
    """+CME ERROR response."""
    transport.feed("+CME ERROR: 10")
    resp = await executor.execute("AT+COPS=?")
    assert resp.success is False
    assert "+CME ERROR: 10" in resp.lines


async def test_cms_error(executor, transport):
    """+CMS ERROR response."""
    transport.feed("+CMS ERROR: 500")
    resp = await executor.execute("AT+CMGS")
    assert resp.success is False
    assert "+CMS ERROR: 500" in resp.lines


async def test_timeout(executor, transport):
    """No response within timeout raises ATTimeoutError."""
    with pytest.raises(ATTimeoutError) as exc_info:
        await executor.execute("AT", timeout=0.1)

    message = str(exc_info.value)
    assert "AT" in message
    assert "OK" in message


async def test_direct_read_timeout_raises_at_timeout_with_command_and_expect(transport, urc):
    """A direct transport read timeout is an AT timeout, not a transport failure."""
    executor = ATCommandExecutor(transport, urc)

    with pytest.raises(ATTimeoutError) as exc_info:
        await executor.execute("AT+CSQ", expect=("READY",), timeout=0.01)

    message = str(exc_info.value)
    assert "AT+CSQ" in message
    assert "READY" in message
    assert transport.last_written == "AT+CSQ\r\n"


async def test_direct_read_transport_oserror_raises_transport_error(transport, urc):
    """Real transport read failures still surface as TransportError."""
    executor = ATCommandExecutor(transport, urc)

    async def raise_oserror():
        raise OSError("serial disconnected")

    transport.readline = raise_oserror

    with pytest.raises(TransportError) as exc_info:
        await executor.execute("AT", timeout=0.1)

    message = str(exc_info.value)
    assert "Transport error during command" in message
    assert "serial disconnected" in message


class EOFTransport(Transport):
    """Transport double that simulates a serial EOF/USB disconnect."""

    def __init__(self):
        self.reads = 0
        self.writes: list[bytes] = []

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def read(self, size: int = -1) -> bytes:
        return b""

    async def readline(self) -> bytes:
        self.reads += 1
        await asyncio.sleep(0)
        return b""

    def in_waiting(self) -> int:
        return 0


async def test_reader_loop_treats_empty_read_as_transport_disconnect(bus):
    """An EOF/empty read stops the reader instead of spinning as blank idle lines."""
    transport = EOFTransport()
    executor = ATCommandExecutor(transport, URCDispatcher(bus))

    await executor.start_reader()

    try:
        for _ in range(20):
            if not executor._reader_active:
                break
            await asyncio.sleep(0.01)

        assert executor._reader_active is False
        assert executor._reader_task is not None
        with pytest.raises(TransportError, match="closed|EOF|empty"):
            executor._reader_task.result()
        assert transport.reads == 1
    finally:
        await executor.stop_reader()


async def test_direct_command_empty_read_raises_transport_error(urc):
    """Command-in-flight EOF is reported as a transport failure, not a timeout."""
    transport = EOFTransport()
    executor = ATCommandExecutor(transport, urc)

    with pytest.raises(TransportError, match="Transport error during command"):
        await executor.execute("AT", timeout=0.05)

    assert transport.writes == [b"AT\r\n"]


async def test_echo_suppression(executor, transport):
    """If modem echoes back the command, it should be skipped."""
    transport.feed("AT+CSQ", "+CSQ: 15,0", "OK")
    resp = await executor.execute("AT+CSQ")
    assert resp.success is True
    # The echoed command should not be in the response lines
    assert "AT+CSQ" not in resp.lines
    assert "+CSQ: 15,0" in resp.lines


async def test_log_command_redacts_sensitive_echoes(executor, transport):
    """A redacted display command should protect TX, RX echoes, and responses."""
    secret_command = 'AT+CPIN="12345678","8765"'
    redacted_command = 'AT+CPIN="<PUK>","<new PIN>"'
    transport.feed(secret_command, "OK")
    log_stream = io.StringIO()
    handler = logging.StreamHandler(log_stream)
    logger = logging.getLogger("callstack.executor")
    old_level = logger.level
    old_propagate = logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    try:
        resp = await executor.execute(secret_command, log_command=redacted_command)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        logger.propagate = old_propagate

    log_text = log_stream.getvalue()
    assert resp.lines == ["OK"]
    assert "12345678" not in log_text
    assert "8765" not in log_text
    assert redacted_command in log_text


async def test_timeout_message_uses_redacted_log_command(executor, transport):
    """Timeout errors should not include sensitive raw command arguments."""
    secret_command = 'AT+CPIN="12345678","8765"'
    redacted_command = 'AT+CPIN="<PUK>","<new PIN>"'

    with pytest.raises(ATTimeoutError) as exc_info:
        await executor.execute(secret_command, timeout=0.01, log_command=redacted_command)

    message = str(exc_info.value)
    assert "12345678" not in message
    assert "8765" not in message
    assert redacted_command in message


async def test_timeout_without_reader_raises_at_timeout_error(transport, urc):
    """Direct-read commands should report command timeouts, not transport errors."""
    executor = ATCommandExecutor(transport, urc)

    with pytest.raises(ATTimeoutError):
        await executor.execute("AT", timeout=0.01)


async def test_urc_during_command(executor, transport, bus):
    """URCs received during command execution are dispatched, not included in response."""
    received_urcs = []

    @bus.on(RingEvent)
    async def on_ring(event):
        received_urcs.append(event)

    transport.feed("RING", "+CSQ: 20,0", "OK")
    resp = await executor.execute("AT+CSQ")

    assert resp.success is True
    # RING should not be in command response
    assert "RING" not in resp.lines
    assert "+CSQ: 20,0" in resp.lines

    await asyncio.sleep(0.01)  # Let the URC handler task run
    assert len(received_urcs) == 1


@pytest.mark.parametrize(
    "terminal_result",
    ["BUSY", "NO CARRIER", "NO ANSWER", "NO DIALTONE", "NO DIAL TONE"],
)
async def test_dial_terminal_result_is_failed_response_not_urc(
    executor, transport, bus, terminal_result
):
    """ATD terminal call results complete the dial command instead of dispatching."""
    received_call_states = []

    @bus.on(CallStateEvent)
    async def on_call_state(event):
        received_call_states.append(event.state)

    transport.feed(terminal_result, "OK")

    resp = await executor.execute("ATD+123****7890;", expect=["OK"], timeout=1.0)

    assert resp.success is False
    assert resp.lines == [terminal_result]
    await asyncio.sleep(0.01)  # Let any incorrectly-dispatched URC task run
    assert received_call_states == []


async def test_idle_no_carrier_still_dispatches_call_state_urc(transport, bus, urc):
    """The ATD special case must not disable idle NO CARRIER URC handling."""
    executor = ATCommandExecutor(transport, urc)
    received_call_states = []

    @bus.on(CallStateEvent)
    async def on_call_state(event):
        received_call_states.append(event.state)

    await executor.start_reader()
    try:
        transport.feed("NO CARRIER")
        for _ in range(10):
            if received_call_states:
                break
            await asyncio.sleep(0.01)
    finally:
        await executor.stop_reader()

    assert received_call_states == [CallState.ENDED]


async def test_blank_lines_skipped(executor, transport):
    """Blank lines between response lines are ignored."""
    transport.feed("", "+CSQ: 20,0", "", "OK")
    resp = await executor.execute("AT+CSQ")
    assert resp.success is True
    assert resp.data_lines == ["+CSQ: 20,0"]


async def test_custom_expect(executor, transport):
    """Custom expected result code (e.g. > for SMS)."""
    transport.feed("> ")
    resp = await executor.execute('AT+CMGS="+1555"', expect=[">"])
    assert resp.success is True


async def test_serialization(executor, transport):
    """Commands are serialized -- only one executes at a time."""
    order = []

    async def feeder():
        """Feed responses with timing that ensures the reader routes them correctly."""
        for _ in range(2):
            await asyncio.sleep(0.02)
            transport.feed("OK")

    asyncio.create_task(feeder())

    async def run_cmd(cmd):
        resp = await executor.execute(cmd, timeout=2.0)
        order.append(cmd)
        return resp

    # Start two commands concurrently — executor lock serializes them
    t1 = asyncio.create_task(run_cmd("AT"))
    t2 = asyncio.create_task(run_cmd("AT+CSQ"))

    r1, r2 = await asyncio.gather(t1, t2)
    assert r1.success is True
    assert r2.success is True
    assert len(order) == 2
