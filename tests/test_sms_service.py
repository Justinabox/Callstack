"""Tests for the SMS service."""

import asyncio
import logging
from datetime import timedelta

import pytest
from callstack.events.bus import EventBus
from callstack.events.types import (
    IncomingSMSEvent,
    SMSSentEvent,
    _RawDeliveryReport,
    _RawSMSNotification,
)
from callstack.errors import SMSSendError
from callstack.protocol.executor import ATCommandExecutor, ATResponse
from callstack.protocol.urc import URCDispatcher
from callstack.transport.mock import MockTransport
from callstack.sms.service import SMSService
from callstack.sms.store import SMSStore
from callstack.sms.types import SMS


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
def executor(transport, urc):
    return ATCommandExecutor(transport, urc)


@pytest.fixture
def store():
    return SMSStore()


@pytest.fixture
def sms_service(executor, bus, store):
    return SMSService(executor, bus, store)


class FailingSMSStore(SMSStore):
    """SMS store test double that fails before accepting a message."""

    async def save(self, sms):
        raise RuntimeError("simulated durable store failure")


# -- Initialization --

async def test_initialize(sms_service, transport):
    """Initialize sends correct AT commands."""
    transport.feed("OK")  # CMGF
    transport.feed("OK")  # CSCS
    transport.feed("OK")  # CNMI
    transport.feed("OK")  # CSMP
    await sms_service.initialize()
    assert sms_service._initialized
    written = transport.all_written
    assert any("CMGF=1" in w for w in written)
    assert any("CSCS" in w for w in written)
    assert 'AT+CNMI=2,1,0,1,0\r\n' in written


async def test_initialize_uses_configured_command_timeout(executor, bus, store):
    """SMS startup commands use the configured base command timeout."""
    calls = []

    async def record_execute(command, expect=("OK",), timeout=5.0):
        calls.append((command, timeout))

    executor.execute = record_execute
    service = SMSService(executor, bus, store, command_timeout=1.75)

    await service.initialize()

    assert calls == [
        ("AT+CMGF=1", 1.75),
        ('AT+CSCS="GSM"', 1.75),
        ("AT+CNMI=2,1,0,1,0", 1.75),
        ("AT+CSMP=49,167,0,0", 1.75),
    ]


async def test_stored_message_management_uses_configured_command_timeout(executor, bus, store):
    """Basic stored-message reads/deletes use the configured command timeout."""
    calls = []

    async def record_execute(command, expect=("OK",), timeout=5.0):
        calls.append((command, timeout))
        return ATResponse(success=True, lines=["OK"])

    executor.execute = record_execute
    service = SMSService(executor, bus, store, command_timeout=1.75)

    await service.list_messages()
    await service.read_message(3)
    await service.delete_message(3)
    await service.delete_all()

    assert calls == [
        ('AT+CMGL="ALL"', 1.75),
        ("AT+CMGR=3", 1.75),
        ("AT+CMGD=3", 1.75),
        ("AT+CMGD=1,4", 1.75),
    ]


async def test_delivery_report_read_and_delete_use_configured_command_timeout(
    executor, bus, store
):
    """Delivery report follow-up reads/deletes use the configured timeout."""
    calls = []

    async def record_execute(command, expect=("OK",), timeout=5.0):
        calls.append((command, timeout))
        return ATResponse(
            success=True,
            lines=[
                '+CMGR: "REC READ",6,"5551234",129,"24/06/25,12:00:00+00","24/06/25,12:00:05+00",0',
                "OK",
            ],
        )

    executor.execute = record_execute
    service = SMSService(executor, bus, store, command_timeout=1.75)

    await service._on_delivery_report(_RawDeliveryReport(storage="SM", index=4))

    assert calls == [
        ("AT+CMGR=4", 1.75),
        ("AT+CMGD=4", 1.75),
    ]


async def test_send_uses_configured_prompt_and_submit_timeouts(executor, bus, store):
    """Outbound SMS prompt and submit phases use explicit send timeout knobs."""
    calls = []

    async def record_execute(command, expect=("OK",), timeout=5.0):
        calls.append(("execute", command, tuple(expect), timeout))
        return ATResponse(success=True, lines=["> "])

    async def record_send_data(data, expect=("OK",), timeout=30.0):
        calls.append(("send_data", data, tuple(expect), timeout))
        return ATResponse(success=True, lines=["+CMGS: 17", "OK"])

    executor.execute = record_execute
    executor.send_data = record_send_data
    service = SMSService(
        executor,
        bus,
        store,
        sms_prompt_timeout=1.25,
        sms_submit_timeout=8.5,
    )

    await service.send("5551234", "Hello")

    assert calls == [
        ("execute", 'AT+CMGS="5551234"', (">",), 1.25),
        ("send_data", b"Hello\x1a", ("+CMGS:", "OK"), 8.5),
    ]


# -- Sending --
async def test_send_success(sms_service, transport, bus):
    """Successful SMS send."""
    sent_events = []

    async def on_sent(e):
        sent_events.append(e)

    bus.subscribe(SMSSentEvent, on_sent)

    # First command: AT+CMGS="number" -> ">"
    transport.feed("> ")
    # Second command: body + Ctrl+Z -> "+CMGS: 42" + "OK"
    transport.feed("+CMGS: 42", "OK")

    sms = await sms_service.send("+15551234", "Hello!")
    assert sms.recipient == "+15551234"
    assert sms.body == "Hello!"
    assert sms.reference == 42
    assert sms.status == "sent"

    await asyncio.sleep(0.01)
    assert len(sent_events) == 1
    assert sent_events[0].reference == 42


async def test_send_info_log_redacts_recipient_number(sms_service, transport, caplog):
    """SMS send logs must not expose the raw destination number."""
    recipient = "+15551234567"
    transport.feed("> ")
    transport.feed("+CMGS: 47", "OK")

    with caplog.at_level(logging.INFO, logger="callstack.sms"):
        await sms_service.send(recipient, "Hello!")

    assert recipient not in caplog.text
    assert "SMS sent to" in caplog.text


async def test_send_gsm_charset_non_ascii_without_replacement(sms_service, transport):
    """GSM text-mode sends GSM 03.38 characters without ASCII replacement."""
    transport.feed("> ")
    transport.feed("+CMGS: 43", "OK")

    sms = await sms_service.send("+15551234567", "Café")

    assert sms.body == "Café"
    assert transport._written[-1] == b"Caf\x05\x1A"
    assert b"?" not in transport._written[-1]


async def test_send_gsm_charset_extension_table_without_literal_ascii(sms_service, transport):
    """GSM text-mode escapes extension-table characters before sending."""
    transport.feed("> ")
    transport.feed("+CMGS: 44", "OK")

    await sms_service.send("+15551234567", "{^}")

    assert transport._written[-1] == b"\x1B\x28\x1B\x14\x1B\x29\x1A"


async def test_send_ucs2_required_text_fails_before_contacting_modem(sms_service, transport):
    """Unsupported text must not be lossy-replaced or sent to the modem."""
    with pytest.raises(SMSSendError, match="cannot be encoded"):
        await sms_service.send("+15551234567", "Code 中")

    assert transport._written == []


async def test_send_reserved_gsm_escape_slot_fails_before_contacting_modem(sms_service, transport):
    """NBSP must not be sent as the raw GSM escape byte."""
    transport.feed("> ")
    transport.feed("+CMGS: 45", "OK")

    with pytest.raises(SMSSendError, match="cannot be encoded"):
        await sms_service.send("+15551234567", "\u00a0")

    assert transport._written == []


async def test_send_gsm_terminator_character_fails_before_contacting_modem(sms_service, transport):
    """Body bytes must never contain Ctrl-Z before the final terminator."""
    transport.feed("> ")
    transport.feed("+CMGS: 46", "OK")

    with pytest.raises(SMSSendError, match="cannot be encoded"):
        await sms_service.send("+15551234567", "Ξ")

    assert transport._written == []


async def test_send_prompt_failure(sms_service, transport):
    """SMS send fails at prompt stage."""
    transport.feed("ERROR")
    with pytest.raises(SMSSendError):
        await sms_service.send("+15551234", "Hello!")


async def test_send_body_failure(sms_service, transport):
    """SMS send fails after body submission."""
    transport.feed("> ")
    transport.feed("ERROR")
    with pytest.raises(SMSSendError):
        await sms_service.send("5551234", "Hello!")


async def test_send_requires_explicit_cmgs_reference(sms_service, transport, store, bus):
    """A final OK without +CMGS must not be reported as a sent SMS."""
    sent_events = []

    async def on_sent(event):
        sent_events.append(event)

    bus.subscribe(SMSSentEvent, on_sent)
    transport.feed("> ")
    transport.feed("OK")

    with pytest.raises(SMSSendError, match="CMGS"):
        await sms_service.send("5551234", "Hello")

    await asyncio.sleep(0.01)
    assert await store.count() == 0
    assert sent_events == []


async def test_send_accepts_explicit_zero_cmgs_reference(sms_service, transport, bus):
    """A modem-provided +CMGS: 0 is a real submit reference, not missing data."""
    sent_events = []

    async def on_sent(event):
        sent_events.append(event)

    bus.subscribe(SMSSentEvent, on_sent)
    transport.feed("> ")
    transport.feed("+CMGS: 0", "OK")

    sms = await sms_service.send("5551234", "Hello")

    await asyncio.sleep(0.01)
    assert sms.reference == 0
    assert len(sent_events) == 1
    assert sent_events[0].reference == 0


async def test_send_stores_message(sms_service, transport, store):
    """Sent message is saved to the store."""
    transport.feed("> ")
    transport.feed("+CMGS: 1", "OK")
    await sms_service.send("+15551234", "Test")
    assert await store.count() == 1
    msg = await store.get(1)
    assert msg.body == "Test"


# -- Receiving via CMTI --

async def test_receive_cmti(sms_service, transport, bus, store):
    """Incoming SMS via +CMTI notification triggers fetch and event."""
    received = []

    # Subscribe to the re-emitted IncomingSMSEvent (not the initial one from URC)
    all_events = []

    async def track(e):
        all_events.append(e)

    bus.subscribe(IncomingSMSEvent, track)

    # When the service gets a CMTI notification, it will call AT+CMGR to fetch,
    # then AT+CMGD to delete the message from SIM storage
    transport.feed('+CMGR: "REC UNREAD","+155****9876","","24/12/25,14:30:00+04"', "Hello there!", "OK")
    transport.feed("OK")  # Response for AT+CMGD (delete after read)

    # Simulate URC dispatch (now uses _RawSMSNotification)
    await bus.emit(_RawSMSNotification(raw='+CMTI: "SM",3'))

    await asyncio.sleep(0.05)
    # The re-emitted enriched event (empty raw, populated sender/body)
    enriched = [e for e in all_events if e.sender == "+155****9876" and not e.raw]
    assert len(enriched) == 1
    assert enriched[0].body == "Hello there!"
    assert [command.strip() for command in transport.all_written] == [
        "AT+CMGR=3",
        "AT+CMGD=3",
    ]


async def test_receive_cmti_store_failure_does_not_delete_sim_slot(executor, transport, bus):
    """If durable store save fails, the SIM slot remains for retry/recovery."""
    service = SMSService(executor, bus, FailingSMSStore())
    transport.feed(
        '+CMGR: "REC UNREAD","+155****9876","","24/12/25,14:30:00+04"',
        "Hello there!",
        "OK",
    )
    # Keep the pre-fix delete path from timing out so RED proves the destructive command.
    transport.feed("OK")

    with pytest.raises(RuntimeError, match="simulated durable store failure"):
        await service._on_incoming(_RawSMSNotification(raw='+CMTI: "SM",3'))

    assert [command.strip() for command in transport.all_written] == ["AT+CMGR=3"]


async def test_receive_cmti_delete_failure_surfaces_cleanup_without_rolling_back(
    sms_service, transport, bus, store, caplog
):
    """Failed SIM cleanup remains visible after local SMS acceptance."""
    sender = "+155****9876"
    body = "private code 123456"
    received = []

    async def track(event):
        received.append(event)

    bus.subscribe(IncomingSMSEvent, track)
    transport.feed(
        f'+CMGR: "REC UNREAD","{sender}","","24/12/25,14:30:00+04"',
        body,
        "OK",
    )
    transport.feed("ERROR")  # AT+CMGD cleanup failed after save.

    with caplog.at_level(logging.INFO, logger="callstack.sms"):
        await sms_service._on_incoming(_RawSMSNotification(raw='+CMTI: "SM",3'))
        await asyncio.sleep(0.01)

    assert await store.count() == 1
    assert len(received) == 1
    assert received[0].sender == sender
    assert received[0].body == body
    assert [command.strip() for command in transport.all_written] == [
        "AT+CMGR=3",
        "AT+CMGD=3",
    ]
    assert "Failed to delete SIM SMS slot after local acceptance" in caplog.text
    assert "storage=SM" in caplog.text
    assert "index=3" in caplog.text
    assert "Incoming SMS from" in caplog.text
    assert sender not in caplog.text
    assert body not in caplog.text


async def test_receive_cmti_retry_for_uncleared_slot_does_not_duplicate_local_sms(
    sms_service, transport, bus, store
):
    """A repeated CMTI for an accepted uncleared slot retries cleanup only."""
    sender = "+155****9876"
    body = "private code 123456"
    received = []

    async def track(event):
        received.append(event)

    bus.subscribe(IncomingSMSEvent, track)
    transport.feed(
        f'+CMGR: "REC UNREAD","{sender}","","24/12/25,14:30:00+04"',
        body,
        "OK",
    )
    transport.feed("ERROR")  # Initial AT+CMGD cleanup failed after save.

    await sms_service._on_incoming(_RawSMSNotification(raw='+CMTI: "SM",3'))
    await asyncio.sleep(0.01)
    assert await store.count() == 1
    assert len(received) == 1

    transport.feed(
        f'+CMGR: "REC UNREAD","{sender}","","24/12/25,14:30:00+04"',
        body,
        "OK",
    )
    transport.feed("OK")  # Retry cleanup for the same already-accepted SMS.
    await sms_service._on_incoming(_RawSMSNotification(raw='+CMTI: "SM",3'))
    await asyncio.sleep(0.01)

    assert await store.count() == 1
    assert len(received) == 1
    assert [command.strip() for command in transport.all_written] == [
        "AT+CMGR=3",
        "AT+CMGD=3",
        "AT+CMGR=3",
        "AT+CMGD=3",
    ]


async def test_receive_cmti_uncleared_slot_reuse_still_accepts_new_sms(
    sms_service, transport, bus, store
):
    """Slot cleanup tracking must not drop a later SMS that reuses the same index."""
    first_sender = "+155****9876"
    second_sender = "+155****5432"
    received = []

    async def track(event):
        received.append(event)

    bus.subscribe(IncomingSMSEvent, track)
    transport.feed(
        f'+CMGR: "REC UNREAD","{first_sender}","","24/12/25,14:30:00+04"',
        "first private code",
        "OK",
    )
    transport.feed("ERROR")
    await sms_service._on_incoming(_RawSMSNotification(raw='+CMTI: "SM",3'))
    await asyncio.sleep(0.01)

    transport.feed(
        f'+CMGR: "REC UNREAD","{second_sender}","","24/12/25,14:35:00+04"',
        "second private code",
        "OK",
    )
    transport.feed("OK")
    await sms_service._on_incoming(_RawSMSNotification(raw='+CMTI: "SM",3'))
    await asyncio.sleep(0.01)

    assert await store.count() == 2
    assert [event.body for event in received] == [
        "first private code",
        "second private code",
    ]
    assert [command.strip() for command in transport.all_written] == [
        "AT+CMGR=3",
        "AT+CMGD=3",
        "AT+CMGR=3",
        "AT+CMGD=3",
    ]


async def test_receive_cmti_delete_exception_surfaces_cleanup_without_private_echo(
    sms_service, transport, bus, store, caplog, monkeypatch
):
    """Delete transport failures are cleanup failures, not SMS receive rollbacks."""
    sender = "+155****9876"
    body = "private code 123456"
    received = []

    async def track(event):
        received.append(event)

    async def fail_delete(index):
        raise TimeoutError(f"timed out deleting {sender} {body}")

    bus.subscribe(IncomingSMSEvent, track)
    monkeypatch.setattr(sms_service, "delete_message", fail_delete)
    transport.feed(
        f'+CMGR: "REC UNREAD","{sender}","","24/12/25,14:30:00+04"',
        body,
        "OK",
    )

    with caplog.at_level(logging.INFO, logger="callstack.sms"):
        await sms_service._on_incoming(_RawSMSNotification(raw='+CMTI: "SM",3'))
        await asyncio.sleep(0.01)

    assert await store.count() == 1
    assert len(received) == 1
    assert "Failed to delete SIM SMS slot after local acceptance" in caplog.text
    assert "TimeoutError" in caplog.text
    assert "Incoming SMS from" in caplog.text
    assert sender not in caplog.text
    assert body not in caplog.text


async def test_receive_cmti_info_log_redacts_sender_number(sms_service, transport, bus, caplog):
    """Stored incoming SMS logs must not expose the raw sender number."""
    sender = "+155****2468"
    body = "stored private code 246810"
    transport.feed(
        f'+CMGR: "REC UNREAD","{sender}","","24/12/25,14:30:00+04"',
        body,
        "OK",
    )
    transport.feed("OK")

    with caplog.at_level(logging.INFO, logger="callstack.sms"):
        await bus.emit(_RawSMSNotification(raw='+CMTI: "SM",7'))
        await asyncio.sleep(0.05)

    assert sender not in caplog.text
    assert body not in caplog.text
    assert "Incoming SMS from" in caplog.text


# -- Receiving via CMT --

async def test_receive_cmt(sms_service, bus, store):
    """Incoming SMS via +CMT direct delivery."""
    all_events = []

    async def track(e):
        all_events.append(e)

    bus.subscribe(IncomingSMSEvent, track)

    await bus.emit(_RawSMSNotification(
        sender="+15559876", body="Direct message", raw='+CMT: "+15559876","","24/12/25,14:30:00+04"'
    ))

    await asyncio.sleep(0.05)
    # The re-emitted enriched event (empty raw, populated body)
    enriched = [e for e in all_events if e.body == "Direct message" and not e.raw]
    assert len(enriched) >= 1
    assert await store.count() == 1


async def test_receive_cmt_info_log_redacts_sender_number(sms_service, bus, caplog):
    """Direct incoming SMS logs must not expose the raw sender number."""
    sender = "+15557654321"

    with caplog.at_level(logging.INFO, logger="callstack.sms"):
        await bus.emit(_RawSMSNotification(
            sender=sender,
            body="private one-time code 123456",
            raw=f'+CMT: "{sender}","","24/12/25,14:30:00+04"',
        ))
        await asyncio.sleep(0.05)

    assert sender not in caplog.text
    assert "private one-time code" not in caplog.text
    assert "Incoming SMS from" in caplog.text


# -- Message Management --

async def test_list_messages(sms_service, transport):
    """List messages from SIM."""
    transport.feed(
        '+CMGL: 0,"REC UNREAD","+155****1111","","24/12/25,10:00:00+04"',
        "Hello",
        '+CMGL: 1,"REC READ","+155****2222","","24/12/25,11:00:00+04"',
        "World",
        "OK",
    )
    messages = await sms_service.list_messages()
    assert len(messages) == 2
    assert messages[0].sender == "+155****1111"
    assert messages[0].body == "Hello"
    assert messages[0].storage_index == 0
    assert messages[0].timestamp.utcoffset() == timedelta(hours=1)
    assert messages[1].sender == "+155****2222"
    assert messages[1].body == "World"
    assert messages[1].timestamp.utcoffset() == timedelta(hours=1)


async def test_list_messages_preserves_signed_timezone_offsets(sms_service, transport):
    """CMGL text-mode timestamps preserve signed GSM quarter-hour offsets."""
    transport.feed(
        '+CMGL: 0,"REC UNREAD","+155****1111","","24/12/25,10:00:00+04"',
        "East",
        '+CMGL: 1,"REC READ","+155****2222","","24/12/25,11:00:00-04"',
        "West",
        "OK",
    )

    messages = await sms_service.list_messages()

    assert len(messages) == 2
    assert messages[0].timestamp.utcoffset() == timedelta(hours=1)
    assert messages[1].timestamp.utcoffset() == -timedelta(hours=1)


async def test_list_messages_preserves_multiline_body(sms_service, transport):
    """CMGL parsing keeps body lines until the next message header."""
    transport.feed(
        '+CMGL: 0,"REC UNREAD","+155****1111","","24/12/25,10:00:00+04"',
        "first line",
        "second line",
        '+CMGL: 1,"REC READ","+155****2222","","24/12/25,11:00:00+04"',
        "world",
        "OK",
    )

    messages = await sms_service.list_messages()

    assert len(messages) == 2
    assert messages[0].body == "first line\nsecond line"
    assert messages[1].body == "world"


async def test_list_messages_preserves_body_line_edge_spaces(sms_service, transport):
    """CMGL text-mode bodies keep leading and trailing body spaces."""
    transport.feed(
        '+CMGL: 0,"REC UNREAD","+155****1111","","24/12/25,10:00:00+04"',
        "  padded code 123  ",
        "OK",
    )

    messages = await sms_service.list_messages()

    assert len(messages) == 1
    assert messages[0].body == "  padded code 123  "


async def test_list_messages_empty(sms_service, transport):
    """List messages when SIM is empty."""
    transport.feed("OK")
    messages = await sms_service.list_messages()
    assert messages == []


async def test_read_message(sms_service, transport):
    """Read a single message."""
    transport.feed(
        '+CMGR: "REC UNREAD","+155****1234","","24/12/25,14:30:00+04"',
        "Test body",
        "OK",
    )
    sms = await sms_service.read_message(0)
    assert sms is not None
    assert sms.sender == "+155****1234"
    assert sms.body == "Test body"
    assert sms.storage_index == 0
    assert sms.timestamp.utcoffset() == timedelta(hours=1)


async def test_read_message_preserves_signed_timezone_offset(sms_service, transport):
    """CMGR text-mode timestamps preserve negative GSM quarter-hour offsets."""
    transport.feed(
        '+CMGR: "REC UNREAD","+155****1234","","24/12/25,14:30:00-04"',
        "Test body",
        "OK",
    )

    sms = await sms_service.read_message(0)

    assert sms is not None
    assert sms.timestamp.utcoffset() == -timedelta(hours=1)


async def test_read_message_preserves_multiline_body(sms_service, transport):
    """CMGR parsing keeps all body lines before the final result code."""
    transport.feed(
        '+CMGR: "REC UNREAD","+155****1234","","24/12/25,14:30:00+04"',
        "first line",
        "second line",
        "OK",
    )

    sms = await sms_service.read_message(0)

    assert sms is not None
    assert sms.body == "first line\nsecond line"


async def test_read_message_preserves_body_line_edge_spaces(sms_service, transport):
    """CMGR text-mode bodies keep leading and trailing body spaces."""
    transport.feed(
        '+CMGR: "REC UNREAD","+155****1234","","24/12/25,14:30:00+04"',
        "  padded code 123  ",
        "OK",
    )

    sms = await sms_service.read_message(0)

    assert sms is not None
    assert sms.body == "  padded code 123  "


async def test_read_message_preserves_ok_with_trailing_space_body_line(sms_service, transport):
    """CMGR body lines that look like padded OK are not final results."""
    transport.feed(
        '+CMGR: "REC UNREAD","+155****1234","","24/12/25,14:30:00+04"',
        "OK ",
        "second line",
        "OK",
    )

    sms = await sms_service.read_message(0)

    assert sms is not None
    assert sms.body == "OK \nsecond line"


async def test_read_message_preserves_blank_and_space_only_body_lines_with_reader(
    sms_service, executor, transport
):
    """Reader-loop CMGR collection keeps blank and all-space body lines."""
    await executor.start_reader()
    try:
        task = asyncio.create_task(sms_service.read_message(0))
        await asyncio.sleep(0)
        transport.feed(
            '+CMGR: "REC UNREAD","+155****1234","","24/12/25,14:30:00+04"',
            "first",
            "",
            "   ",
            "second",
            "OK",
        )

        sms = await task
    finally:
        await executor.stop_reader()

    assert sms is not None
    assert sms.body == "first\n\n   \nsecond"


async def test_read_message_normalizes_leading_padded_header(sms_service, transport):
    """CMGR headers remain control lines even with leading modem whitespace."""
    transport.feed(
        '  +CMGR: "REC UNREAD","+155****1234","","24/12/25,14:30:00+04"',
        "body",
        "OK",
    )

    sms = await sms_service.read_message(0)

    assert sms is not None
    assert sms.body == "body"


async def test_read_message_preserves_cmgl_shaped_body_line(sms_service, transport):
    """CMGR parsing treats +CMGL-shaped text as body content."""
    transport.feed(
        '+CMGR: "REC UNREAD","+155****1234","","24/12/25,14:30:00+04"',
        "carrier copied diagnostic:",
        '+CMGL: 9,"REC READ","+155****9999","","24/12/25,15:00:00+04"',
        "OK",
    )

    sms = await sms_service.read_message(0)

    assert sms is not None
    assert sms.body == (
        "carrier copied diagnostic:\n"
        '+CMGL: 9,"REC READ","+155****9999","","24/12/25,15:00:00+04"'
    )


async def test_read_message_preserves_urc_shaped_body_line(sms_service, transport):
    """CMGR command responses keep +CMTI-shaped text as SMS body content."""
    transport.feed(
        '+CMGR: "REC UNREAD","+155****1234","","24/12/25,14:30:00+04"',
        "carrier copied notification:",
        '+CMTI: "SM",99',
        "OK",
    )

    sms = await sms_service.read_message(7)

    assert sms is not None
    assert sms.body == "carrier copied notification:\n+CMTI: \"SM\",99"


async def test_read_message_not_found(sms_service, transport):
    """Read nonexistent message returns None."""
    transport.feed("ERROR")
    sms = await sms_service.read_message(99)
    assert sms is None


async def test_delete_message(sms_service, transport):
    """Delete a message from SIM."""
    transport.feed("OK")
    assert await sms_service.delete_message(0)


async def test_delete_all(sms_service, transport):
    """Delete all messages from SIM."""
    transport.feed("OK")
    assert await sms_service.delete_all()


# -- Subscription API --

async def test_on_message_callback(sms_service, bus):
    """on_message registers a handler for incoming SMS."""
    received = []

    async def handler(event):
        received.append(event)

    sms_service.on_message(handler)

    await bus.emit(IncomingSMSEvent(sender="+1555", body="callback test"))

    await asyncio.sleep(0.01)
    assert len(received) == 1
    assert received[0].body == "callback test"


async def test_on_message_filtered(sms_service, bus):
    """on_message with filter_sender only fires for matching sender."""
    received = []

    async def handler(e):
        received.append(e)

    sms_service.on_message(handler, filter_sender="+1AAA")

    await bus.emit(IncomingSMSEvent(sender="+1BBB", body="wrong"))
    await bus.emit(IncomingSMSEvent(sender="+1AAA", body="right"))

    await asyncio.sleep(0.01)
    assert len(received) == 1
    assert received[0].body == "right"


async def test_messages_async_iterator(sms_service, bus):
    """messages() yields an async iterator of incoming SMS events."""
    results = []

    async def reader():
        async with sms_service.messages() as inbox:
            async for msg in inbox:
                results.append(msg)
                if len(results) >= 2:
                    break

    task = asyncio.create_task(reader())

    await asyncio.sleep(0.01)
    await bus.emit(IncomingSMSEvent(sender="A", body="one", raw="+CMT:"))
    await bus.emit(IncomingSMSEvent(sender="B", body="two", raw="+CMT:"))

    await asyncio.wait_for(task, timeout=1.0)
    assert len(results) == 2
    assert results[0].body == "one"
    assert results[1].body == "two"


async def test_messages_filtered_iterator(sms_service, bus):
    """messages(filter_sender=...) only yields matching events."""
    results = []

    async def reader():
        async with sms_service.messages(filter_sender="A") as inbox:
            async for msg in inbox:
                results.append(msg)
                if len(results) >= 1:
                    break

    task = asyncio.create_task(reader())

    await asyncio.sleep(0.01)
    await bus.emit(IncomingSMSEvent(sender="B", body="skip", raw="+CMT:"))
    await asyncio.sleep(0.01)
    await bus.emit(IncomingSMSEvent(sender="A", body="match", raw="+CMT:"))

    await asyncio.wait_for(task, timeout=1.0)
    assert len(results) == 1
    assert results[0].body == "match"


# -- Parsing --

async def test_parse_timestamp():
    """Timestamps in various formats are handled."""
    from callstack.sms.service import _parse_timestamp
    ts = _parse_timestamp("24/12/25,14:30:00+04")
    assert ts is not None
    assert ts.year == 2024
    assert ts.month == 12
    assert ts.day == 25
    assert ts.hour == 14
    assert ts.minute == 30
    assert ts.utcoffset() == timedelta(hours=1)

    negative = _parse_timestamp("24/12/25,14:30:00-04")
    assert negative is not None
    assert negative.utcoffset() == -timedelta(hours=1)

    no_offset = _parse_timestamp("24/12/25,14:30:00")
    assert no_offset is not None
    assert no_offset.tzinfo is None

    assert _parse_timestamp("") is None
    assert _parse_timestamp("invalid") is None
    assert _parse_timestamp("24/12/25,14:30:00+99") is None
