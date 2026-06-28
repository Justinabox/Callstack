"""Tests for the AT command executor."""

import asyncio
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


async def test_sms_read_debug_log_does_not_expose_raw_cmgr_payload(executor, transport, caplog):
    """Debug logs for SMS reads must not expose raw report/SMS payloads."""
    raw_report = (
        '+CMGR: "REC READ",6,"+155****4567",145,'
        '"24/12/25,14:30:00+04","24/12/25,14:30:05+04",0'
    )

    with caplog.at_level(logging.DEBUG, logger="callstack.executor"):
        transport.feed(raw_report, "OK")
        resp = await executor.execute("AT+CMGR=9")

    assert resp.success is True
    assert resp.data_lines == [raw_report]
    assert "+155****4567" not in caplog.text
    assert raw_report not in caplog.text
    assert "RX: <redacted SMS read response>" in caplog.text


async def test_registration_debug_log_does_not_expose_cell_identifiers(executor, transport, caplog):
    """Debug logs for registration queries must not expose TAC/cell IDs."""
    raw_registration = '+CEREG: 2,1,"ABCD","12345678",7'

    with caplog.at_level(logging.DEBUG, logger="callstack.executor"):
        transport.feed(raw_registration, "OK")
        resp = await executor.execute("AT+CEREG?")

    assert resp.success is True
    assert "ABCD" not in caplog.text
    assert "12345678" not in caplog.text
    assert raw_registration not in caplog.text
    assert "RX: +CEREG: status=1" in caplog.text


async def test_sensitive_cpin_command_is_redacted_from_debug_logs(executor, transport, caplog):
    """SIM PIN commands must not expose credentials in TX or echoed RX logs."""
    command = 'AT+CPIN="1234"'
    transport.feed(command, "OK")

    with caplog.at_level(logging.DEBUG, logger="callstack.executor"):
        resp = await executor.execute(command)

    assert resp.success is True
    assert "1234" not in caplog.text
    assert command not in caplog.text
    assert "AT+CPIN=<redacted>" in caplog.text


async def test_sensitive_cpin_command_is_redacted_from_timeout_error(transport, urc):
    """SIM PIN commands must not expose credentials in timeout diagnostics."""
    executor = ATCommandExecutor(transport, urc)
    command = 'AT+CPIN="1234"'

    with pytest.raises(ATTimeoutError) as exc_info:
        await executor.execute(command, timeout=0.01)

    message = str(exc_info.value)
    assert "1234" not in message
    assert command not in message
    assert "AT+CPIN=<redacted>" in message


async def test_delayed_sensitive_cpin_echo_is_redacted_in_idle_reader(executor, transport, caplog):
    """Late SIM PIN command echoes must not leak after command timeout."""
    command = 'AT+CPIN="1234"'

    with caplog.at_level(logging.DEBUG, logger="callstack.executor"):
        with pytest.raises(ATTimeoutError):
            await executor.execute(command, timeout=0.01)
        transport.feed(command)
        await asyncio.sleep(0)

    assert "1234" not in caplog.text
    assert command not in caplog.text
    assert "Ignoring non-URC idle line: AT+CPIN=<redacted>" in caplog.text


async def test_delayed_sensitive_cpin_echo_does_not_leak_into_next_command(executor, transport, caplog):
    """A stale SIM PIN echo must not leak while a later command is in flight."""
    command = 'AT+CPIN="1234"'

    async def feed_next_command_response():
        await asyncio.sleep(0)
        transport.feed(command, "+CSQ: 20,0", "OK")

    with caplog.at_level(logging.DEBUG, logger="callstack.executor"):
        with pytest.raises(ATTimeoutError):
            await executor.execute(command, timeout=0.01)
        feeder = asyncio.create_task(feed_next_command_response())
        resp = await executor.execute("AT+CSQ", timeout=1.0)
        await feeder

    assert resp.lines == ["+CSQ: 20,0", "OK"]
    assert "1234" not in caplog.text
    assert command not in caplog.text
    assert "AT+CPIN=<redacted>" in caplog.text


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


@pytest.mark.parametrize(
    "terminal_result", ["NO CARRIER", "NO DIALTONE", "NO DIAL TONE"]
)
async def test_idle_call_terminal_result_dispatches_call_state_urc(
    transport, bus, urc, terminal_result
):
    """The ATD special case must not disable idle call-ended URC handling."""
    executor = ATCommandExecutor(transport, urc)
    received_call_states = []

    @bus.on(CallStateEvent)
    async def on_call_state(event):
        received_call_states.append(event.state)

    await executor.start_reader()
    try:
        transport.feed(terminal_result)
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
