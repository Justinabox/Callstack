"""Tests for the SMS service."""

import asyncio
import logging
from datetime import timedelta

import pytest
from callstack.events.bus import EventBus
from callstack.events.types import (
    IncomingSMSEvent,
    RingEvent,
    SMSSentEvent,
    _RawDeliveryReport,
    _RawSMSNotification,
)
from callstack.errors import SMSPersistenceError, SMSSendError
from callstack.protocol.executor import ATCommandExecutor, ATResponse
from callstack.protocol.urc import URCDispatcher
from callstack.transport.mock import MockTransport
from callstack.sms.pdu import MultipartInfo, PDUEncoder
from callstack.sms.service import SMSService
from callstack.sms.store import SMSStore
from callstack.sms.types import SMS


def _numeric_deliver_pdu(sender: str = "5550123", body: str = "Hi") -> str:
    """Build a single-part numeric-originator SMS-DELIVER PDU for tests."""
    sender_encoded, toa = PDUEncoder.encode_phone_number(sender)
    body_packed, body_len = PDUEncoder.encode_gsm7(body)
    return (
        "00"  # SCA: use default SMSC
        "04"  # SMS-DELIVER
        f"{len(sender.lstrip('+')):02X}"
        f"{toa:02X}"
        f"{sender_encoded}"
        "00"  # PID
        "00"  # DCS: GSM 7-bit default alphabet
        "42215241030040"  # SCTS
        f"{body_len:02X}"
        f"{body_packed.hex().upper()}"
    )


def _pack_gsm7_user_data_with_udh(udh: bytes, body: str) -> tuple:
    """Pack GSM-7 user data with a byte-aligned UDH for inbound PDU tests."""
    header_septets = (len(udh) * 8 + 6) // 7
    payload, payload_septets = PDUEncoder.encode_gsm7(body)
    packed = bytearray((header_septets * 7 + payload_septets * 7 + 7) // 8)
    packed[:len(udh)] = udh
    for bit in range(payload_septets * 7):
        if payload[bit // 8] & (1 << (bit % 8)):
            target_bit = header_septets * 7 + bit
            packed[target_bit // 8] |= 1 << (target_bit % 8)
    return bytes(packed), header_septets + payload_septets


def _numeric_deliver_pdu_with_udh(udh: bytes, body: str, sender: str = "5550123") -> str:
    """Build a concatenated-part numeric-originator SMS-DELIVER PDU for tests."""
    sender_encoded, toa = PDUEncoder.encode_phone_number(sender)
    user_data, user_data_length = _pack_gsm7_user_data_with_udh(udh, body)
    return (
        "00"  # SCA: use default SMSC
        "44"  # SMS-DELIVER with UDHI
        f"{len(sender):02X}"
        f"{toa:02X}"
        f"{sender_encoded}"
        "00"  # PID
        "00"  # DCS: GSM 7-bit default alphabet
        "42215241030040"  # SCTS
        f"{user_data_length:02X}"
        f"{user_data.hex().upper()}"
    )


def _alphanumeric_deliver_pdu(sender: str = "ACME/OTP", body: str = "Hi") -> str:
    """Build a single-part alphanumeric-originator SMS-DELIVER PDU for tests."""
    sender_packed, sender_len = PDUEncoder.encode_gsm7(sender)
    body_packed, body_len = PDUEncoder.encode_gsm7(body)
    return (
        "00"  # SCA: use default SMSC
        "04"  # SMS-DELIVER
        f"{sender_len:02X}"
        "D0"  # TON: alphanumeric
        f"{sender_packed.hex().upper()}"
        "00"  # PID
        "00"  # DCS: GSM 7-bit default alphabet
        "42215241030040"  # SCTS
        f"{body_len:02X}"
        f"{body_packed.hex().upper()}"
    )


def _numeric_ucs2_deliver_pdu(user_data: bytes, *, udl: int | None = None) -> str:
    """Build a numeric-originator UCS2 SMS-DELIVER PDU for reject-path tests."""
    sender = "5550123"
    sender_encoded, toa = PDUEncoder.encode_phone_number(sender)
    return (
        "00"  # SCA: use default SMSC
        "04"  # SMS-DELIVER
        f"{len(sender):02X}"
        f"{toa:02X}"
        f"{sender_encoded}"
        "00"  # PID
        "08"  # DCS: UCS2
        "42215241030040"  # SCTS
        f"{len(user_data) if udl is None else udl:02X}"
        f"{user_data.hex().upper()}"
    )


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


class FailOnceSMSStore(SMSStore):
    """Fails precisely once so a raw PDU replay can prove retry eligibility."""

    def __init__(self):
        super().__init__()
        self._fail_next_save = True

    async def save(self, sms):
        if self._fail_next_save:
            self._fail_next_save = False
            raise RuntimeError("simulated durable store failure")
        return await super().save(sms)


class BlockingSMSStore(SMSStore):
    """Pauses the first save, creating a deterministic concurrent-ingest race."""

    def __init__(self):
        super().__init__()
        self.save_started = asyncio.Event()
        self.release_save = asyncio.Event()
        self.save_calls = 0

    async def save(self, sms):
        self.save_calls += 1
        self.save_started.set()
        await self.release_save.wait()
        return await super().save(sms)


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


async def test_send_store_failure_after_cmgs_reports_partial_success_and_emits_event(
    executor, transport, bus, caplog
):
    """A post-+CMGS store failure is submitted-but-not-persisted, not send failure."""
    service = SMSService(executor, bus, FailingSMSStore())
    sent_events = []
    recipient = "5551234"

    async def on_sent(event):
        sent_events.append(event)

    bus.subscribe(SMSSentEvent, on_sent)
    transport.feed("> ")
    transport.feed("+CMGS: 42", "OK")

    with caplog.at_level(logging.WARNING, logger="callstack.sms"):
        with pytest.raises(SMSPersistenceError) as excinfo:
            await service.send(recipient, "private code 123456")

    await asyncio.sleep(0.01)
    assert excinfo.value.reference == 42
    assert excinfo.value.recipient == recipient
    assert excinfo.value.sms.reference == 42
    assert excinfo.value.sms.body == "private code 123456"
    assert len(sent_events) == 1
    assert sent_events[0].reference == 42
    assert sent_events[0].recipient == recipient
    assert "accepted by modem but was not persisted" in caplog.text
    assert recipient not in caplog.text
    assert "123456" not in caplog.text
    assert "simulated durable store failure" not in caplog.text


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


async def test_receive_cmti_serializes_concurrent_handlers(sms_service, bus, store, monkeypatch):
    """Concurrent notifications for one SIM slot accept the SMS only once."""
    first_read_started = asyncio.Event()
    release_first_read = asyncio.Event()
    concurrent_read_started = asyncio.Event()
    slot_deleted = False
    received = []
    read_calls = 0
    delete_calls = 0
    sms = SMS(sender="+155****9876", body="one message")

    async def read_message(index):
        nonlocal read_calls
        read_calls += 1
        if read_calls == 1:
            first_read_started.set()
            await release_first_read.wait()
            return sms
        if not slot_deleted:
            concurrent_read_started.set()
            return sms
        return None

    async def delete_message(index):
        nonlocal slot_deleted, delete_calls
        delete_calls += 1
        slot_deleted = True
        return True

    bus.subscribe(IncomingSMSEvent, received.append)
    monkeypatch.setattr(sms_service, "read_message", read_message)
    monkeypatch.setattr(sms_service, "delete_message", delete_message)

    first = asyncio.create_task(
        sms_service._on_incoming(_RawSMSNotification(raw='+CMTI: "SM",3'))
    )
    await first_read_started.wait()
    second = asyncio.create_task(
        sms_service._on_incoming(_RawSMSNotification(raw='+CMTI: "SM",3'))
    )

    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(concurrent_read_started.wait(), timeout=0.05)
    finally:
        release_first_read.set()
        await asyncio.gather(first, second)

    assert await store.count() == 1
    assert len(received) == 1
    assert delete_calls == 1


async def test_receive_cmti_accepts_optional_comma_whitespace(sms_service, transport, bus):
    """Spaced +CMTI notifications still fetch and emit the stored SMS."""
    received = []

    async def track(event):
        received.append(event)

    bus.subscribe(IncomingSMSEvent, track)
    transport.feed(
        '+CMGR: "REC UNREAD","+155****9876","","24/12/25,14:30:00+04"',
        "Hello there!",
        "OK",
    )
    transport.feed("OK")

    await sms_service._on_incoming(_RawSMSNotification(raw='+CMTI: "SM", 3'))
    await asyncio.sleep(0.01)

    assert len(received) == 1
    assert received[0].body == "Hello there!"
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
        sender="+155****9876", body="Direct message", raw='+CMT: "+155****9876","","24/12/25,14:30:00+04"'
    ))

    await asyncio.sleep(0.05)
    # The re-emitted enriched event (empty raw, populated body)
    enriched = [e for e in all_events if e.body == "Direct message" and not e.raw]
    assert len(enriched) >= 1
    assert await store.count() == 1


async def test_receive_cmt_store_failure_still_emits_event(executor, bus, caplog):
    """Direct +CMT delivery still emits IncomingSMSEvent when durable store save fails."""
    service = SMSService(executor, bus, FailingSMSStore())
    sender = "+155****9876"
    body = "private code 123456"
    received = []

    async def track(event):
        received.append(event)

    bus.subscribe(IncomingSMSEvent, track)

    with caplog.at_level(logging.WARNING, logger="callstack.sms"):
        await service._on_incoming(
            _RawSMSNotification(
                sender=sender,
                body=body,
                raw=f'+CMT: "{sender}","","24/12/25,14:30:00+04"',
            )
        )
        await asyncio.sleep(0.01)

    assert len(received) == 1
    assert received[0].sender == sender
    assert received[0].body == body
    assert "RuntimeError" in caplog.text
    assert sender not in caplog.text
    assert body not in caplog.text
    assert "simulated durable store failure" not in caplog.text


async def test_executor_direct_cmt_preserves_multiline_body(
    executor, transport, bus, store
):
    """Direct +CMT messages preserve body lines delivered before the idle boundary."""
    SMSService(executor, bus, store)
    body = "first line\nsecond line"

    async with bus.stream(IncomingSMSEvent) as incoming:
        await executor.start_reader()
        try:
            transport.feed(
                '+CMT: "+155****9876","","24/12/25,14:30:00+04"',
                "first line",
                "second line",
            )

            event = await incoming.next(timeout=1.0)
        finally:
            await executor.stop_reader()

    assert event is not None
    assert event.body == body
    messages = await store.list()
    assert [message.body for message in messages] == [body]


async def test_executor_direct_cmt_stops_body_at_next_urc_boundary(
    executor, transport, bus, store
):
    """A following URC is dispatched separately instead of appended to a +CMT body."""
    SMSService(executor, bus, store)

    async with bus.stream(IncomingSMSEvent) as incoming, bus.stream(RingEvent) as rings:
        await executor.start_reader()
        try:
            transport.feed(
                '+CMT: "+155****9876","","24/12/25,14:30:00+04"',
                "only body line",
                "RING",
            )

            sms_event = await incoming.next(timeout=1.0)
            ring_event = await rings.next(timeout=1.0)
        finally:
            await executor.stop_reader()

    assert sms_event is not None
    assert sms_event.body == "only body line"
    assert ring_event is not None
    messages = await store.list()
    assert [message.body for message in messages] == ["only body line"]


class NoInWaitingTransport(MockTransport):
    """Mock transport that mimics SerialTransport's unavailable in_waiting count."""

    def in_waiting(self) -> int:
        return 0


async def test_executor_direct_cmt_preserves_multiline_body_without_in_waiting(
    bus, store
):
    """Direct +CMT multiline bodies do not depend on transport in_waiting support."""
    transport = NoInWaitingTransport()
    executor = ATCommandExecutor(transport, URCDispatcher(bus))
    SMSService(executor, bus, store)
    body = "first line\nsecond line"

    async with bus.stream(IncomingSMSEvent) as incoming:
        await executor.start_reader()
        try:
            transport.feed(
                '+CMT: "+155****9876","","24/12/25,14:30:00+04"',
                "first line",
                "second line",
            )

            event = await incoming.next(timeout=1.0)
        finally:
            await executor.stop_reader()

    assert event is not None
    assert event.body == body
    assert [message.body for message in await store.list()] == [body]


class AutoOKOnWriteTransport(MockTransport):
    """Mock transport that responds OK when a command is written."""

    def __init__(self):
        super().__init__()
        self.body_line_read = asyncio.Event()

    async def readline(self) -> bytes:
        line = await super().readline()
        if line == b"body\r\n":
            self.body_line_read.set()
        return line

    async def write(self, data: bytes) -> None:
        await super().write(data)
        self.feed("OK")


async def test_direct_cmt_continuation_wait_does_not_steal_command_response(bus, store):
    """A command response arriving after a +CMT body is not appended to that SMS."""
    transport = AutoOKOnWriteTransport()
    executor = ATCommandExecutor(transport, URCDispatcher(bus))
    SMSService(executor, bus, store)

    async with bus.stream(IncomingSMSEvent) as incoming:
        await executor.start_reader()
        try:
            transport.feed(
                '+CMT: "+155****9876","","24/12/25,14:30:00+04"',
                "body",
            )
            await asyncio.wait_for(transport.body_line_read.wait(), timeout=1.0)

            response = await executor.execute("AT", timeout=0.1)
            sms_event = await incoming.next(timeout=1.0)
        finally:
            await executor.stop_reader()

    assert response.success is True
    assert sms_event is not None
    assert sms_event.body == "body"
    assert [message.body for message in await store.list()] == ["body"]


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


# -- Receiving via raw PDU --

async def test_ingest_pdu_single_part_persists_and_emits_event(sms_service, bus, store):
    """A valid single-part SMS-DELIVER PDU is persisted and emitted once."""
    received = []

    async def track(event):
        received.append(event)

    bus.subscribe(IncomingSMSEvent, track)

    sms = await sms_service.ingest_pdu(_numeric_deliver_pdu(sender="5550123", body="Hi"))

    await asyncio.sleep(0.01)
    assert sms is not None
    assert sms.sender == "5550123"
    assert sms.body == "Hi"
    assert await store.count() == 1
    assert len(received) == 1
    assert received[0].sender == "5550123"
    assert received[0].body == "Hi"


async def test_ingest_pdu_store_failure_fails_closed_without_private_log(executor, bus, caplog):
    """Raw PDU delivery is not publicly accepted before durable persistence succeeds."""
    service = SMSService(executor, bus, FailingSMSStore())
    sender = "5550123"
    body = "private one-time code 123456"
    pdu = _numeric_deliver_pdu(sender=sender, body=body)

    async with bus.stream(IncomingSMSEvent) as incoming:
        with caplog.at_level(logging.WARNING, logger="callstack.sms"):
            result = await service.ingest_pdu(pdu)
            event = await incoming.next(timeout=0.01)

    assert result is None
    assert event is None
    assert "Failed to persist direct PDU delivery" in caplog.text
    assert "RuntimeError" in caplog.text
    assert sender not in caplog.text
    assert "123456" not in caplog.text
    assert "simulated durable store failure" not in caplog.text


async def test_ingest_pdu_single_part_concurrent_replay_persists_and_emits_once(executor, bus):
    """Concurrent exact single-PDU replays have one durable/public acceptance."""
    store = BlockingSMSStore()
    service = SMSService(executor, bus, store)
    received = []

    async def track(event):
        received.append(event)

    bus.subscribe(IncomingSMSEvent, track)
    pdu = _numeric_deliver_pdu(body="Race")
    first = asyncio.create_task(service.ingest_pdu(pdu))
    await asyncio.wait_for(store.save_started.wait(), timeout=1.0)
    second = asyncio.create_task(service.ingest_pdu(pdu))
    await asyncio.sleep(0)
    store.release_save.set()
    results = await asyncio.gather(first, second)
    await asyncio.sleep(0.01)

    assert sum(result is not None for result in results) == 1
    assert store.save_calls == 1
    assert await store.count() == 1
    assert [event.body for event in received] == ["Race"]


async def test_ingest_pdu_multipart_replay_retries_after_persistence_failure(executor, bus):
    """A failed completed assembly is not tombstoned and can be fully replayed."""
    store = FailOnceSMSStore()
    service = SMSService(executor, bus, store)
    received = []

    async def track(event):
        received.append(event)

    bus.subscribe(IncomingSMSEvent, track)
    part_one = _numeric_deliver_pdu_with_udh(bytes.fromhex("0500037A0201"), "Hello")
    part_two = _numeric_deliver_pdu_with_udh(bytes.fromhex("0500037A0202"), "World")

    assert await service.ingest_pdu(part_one) is None
    assert await service.ingest_pdu(part_two) is None
    await asyncio.sleep(0.01)
    assert received == []

    assert await service.ingest_pdu(part_one) is None
    result = await service.ingest_pdu(part_two)
    await asyncio.sleep(0.01)

    assert result is not None
    assert result.body == "HelloWorld"
    assert await store.count() == 1
    assert [event.body for event in received] == ["HelloWorld"]


@pytest.mark.parametrize("user_data,udl", [(b"\x00", 1), (bytes.fromhex("D800"), 2)])
async def test_ingest_pdu_rejects_malformed_ucs2_without_side_effects(
    sms_service, bus, store, user_data, udl
):
    """Malformed UCS2 cannot become replacement text at the raw ingress boundary."""
    received = []

    async def track(event):
        received.append(event)

    bus.subscribe(IncomingSMSEvent, track)

    assert await sms_service.ingest_pdu(_numeric_ucs2_deliver_pdu(user_data, udl=udl)) is None
    await asyncio.sleep(0.01)

    assert await store.count() == 0
    assert received == []


@pytest.mark.parametrize("pdu", ["not-hex", "00" + ("AA" * 188)])
async def test_ingest_pdu_rejects_invalid_or_oversized_input_before_decoder(
    sms_service, monkeypatch, pdu
):
    """Raw ingress bounds/validates input before handing it to the slicing decoder."""
    def decoder_must_not_run(_pdu):
        raise AssertionError("decoder must not run")

    monkeypatch.setattr(
        "callstack.sms.service.PDUDecoder.decode_deliver_pdu", decoder_must_not_run
    )

    assert await sms_service.ingest_pdu(pdu) is None


async def test_ingest_pdu_alphanumeric_sender_is_not_logged(sms_service, caplog):
    """Parser-accepted alphanumeric originators never cross the logging boundary."""
    sender = "ACME/OTP"
    body = "login token"
    raw_pdu = _alphanumeric_deliver_pdu(sender=sender, body=body)
    caplog.set_level(logging.INFO, logger="callstack.sms")

    result = await sms_service.ingest_pdu(raw_pdu)

    assert result is not None
    assert result.sender == sender
    assert result.body == body
    assert sender not in caplog.text
    assert body not in caplog.text
    assert raw_pdu not in caplog.text


async def test_ingest_pdu_malformed_fails_closed(sms_service, bus, store, caplog):
    """A malformed PDU is rejected without persistence, emission, or PII leaks."""
    received = []

    async def track(event):
        received.append(event)

    bus.subscribe(IncomingSMSEvent, track)
    malformed_pdu = "00"

    with caplog.at_level(logging.WARNING, logger="callstack.sms"):
        result = await sms_service.ingest_pdu(malformed_pdu)
        await asyncio.sleep(0.01)

    assert result is None
    assert await store.count() == 0
    assert received == []
    assert malformed_pdu not in caplog.text


async def test_ingest_pdu_rejects_non_string_input_without_side_effects(sms_service, bus, store):
    """Invalid PDU types must fail closed rather than surfacing parser errors."""
    received = []

    async def track(event):
        received.append(event)

    bus.subscribe(IncomingSMSEvent, track)

    result = await sms_service.ingest_pdu(None)
    await asyncio.sleep(0.01)

    assert result is None
    assert await store.count() == 0
    assert received == []


async def test_ingest_pdu_rejects_non_deliver_tpdu_without_side_effects(sms_service, bus, store):
    """Non-DELIVER TPDUs are not inbound messages and must fail closed."""
    received = []

    async def track(event):
        received.append(event)

    bus.subscribe(IncomingSMSEvent, track)
    submit_pdu = "00" + "01" + _numeric_deliver_pdu()[4:]

    result = await sms_service.ingest_pdu(submit_pdu)
    await asyncio.sleep(0.01)

    assert result is None
    assert await store.count() == 0
    assert received == []


async def test_ingest_pdu_rejects_reserved_dcs_without_side_effects(sms_service, bus, store):
    """Reserved alphabet DCS values must fail closed rather than decode as GSM-7."""
    received = []

    async def track(event):
        received.append(event)

    bus.subscribe(IncomingSMSEvent, track)
    deliver_pdu = _numeric_deliver_pdu()
    reserved_dcs_pdu = deliver_pdu[:18] + "0C" + deliver_pdu[20:]

    result = await sms_service.ingest_pdu(reserved_dcs_pdu)
    await asyncio.sleep(0.01)

    assert result is None
    assert await store.count() == 0
    assert received == []


async def test_ingest_pdu_accepts_gsm7_message_class_dcs(sms_service, bus, store):
    """Classed GSM-7 PDUs are valid and must not be mistaken for compressed data."""
    deliver_pdu = _numeric_deliver_pdu(body="Hi")
    classed_gsm7_pdu = deliver_pdu[:18] + "F1" + deliver_pdu[20:]

    result = await sms_service.ingest_pdu(classed_gsm7_pdu)

    assert result is not None
    assert result.body == "Hi"
    assert await store.count() == 1


async def test_ingest_pdu_rejects_udhi_for_unsupported_non_gsm7_encoding(sms_service, bus, store):
    """UDHI requires a supported GSM-7 path; it must not be silently ignored."""
    received = []

    async def track(event):
        received.append(event)

    bus.subscribe(IncomingSMSEvent, track)
    deliver_pdu = _numeric_deliver_pdu()
    unsupported_pdu = deliver_pdu[:2] + "44" + deliver_pdu[4:18] + "08" + deliver_pdu[20:]

    result = await sms_service.ingest_pdu(unsupported_pdu)
    await asyncio.sleep(0.01)

    assert result is None
    assert await store.count() == 0
    assert received == []


async def test_ingest_pdu_rejects_invalid_timestamp_without_side_effects(sms_service, bus, store):
    """A malformed SCTS must not be replaced with a current timestamp."""
    received = []

    async def track(event):
        received.append(event)

    bus.subscribe(IncomingSMSEvent, track)
    deliver_pdu = _numeric_deliver_pdu()
    invalid_timestamp_pdu = deliver_pdu[:20] + "FFFFFFFFFFFFFF" + deliver_pdu[34:]

    result = await sms_service.ingest_pdu(invalid_timestamp_pdu)
    await asyncio.sleep(0.01)

    assert result is None
    assert await store.count() == 0
    assert received == []


async def test_ingest_pdu_rejects_invalid_timestamp_timezone_bcd(sms_service, bus, store):
    """Invalid SCTS timezone digits must not become a fabricated timestamp."""
    received = []

    async def track(event):
        received.append(event)

    bus.subscribe(IncomingSMSEvent, track)
    deliver_pdu = _numeric_deliver_pdu()
    invalid_timezone_pdu = deliver_pdu[:32] + "AF" + deliver_pdu[34:]

    result = await sms_service.ingest_pdu(invalid_timezone_pdu)
    await asyncio.sleep(0.01)

    assert result is None
    assert await store.count() == 0
    assert received == []


async def test_ingest_pdu_rejects_compressed_udhi_parts_without_side_effects(sms_service, bus, store):
    """Compressed UDH payloads are unsupported and must not look like plaintext."""
    received = []

    async def track(event):
        received.append(event)

    bus.subscribe(IncomingSMSEvent, track)
    udh_seq1 = bytes.fromhex("0500037A0201")
    udh_seq2 = bytes.fromhex("0500037A0202")
    compressed_part_one = _numeric_deliver_pdu_with_udh(udh_seq1, "Hello")
    compressed_part_two = _numeric_deliver_pdu_with_udh(udh_seq2, "World")
    compressed_part_one = compressed_part_one[:18] + "20" + compressed_part_one[20:]
    compressed_part_two = compressed_part_two[:18] + "20" + compressed_part_two[20:]

    assert await sms_service.ingest_pdu(compressed_part_one) is None
    assert await sms_service.ingest_pdu(compressed_part_two) is None
    await asyncio.sleep(0.01)

    assert await store.count() == 0
    assert received == []


async def test_ingest_pdu_concatenated_parts_reassemble_out_of_order(sms_service, bus, store):
    """Out-of-order concatenated GSM-7 parts are buffered and released once complete."""
    received = []

    async def track(event):
        received.append(event)

    bus.subscribe(IncomingSMSEvent, track)
    udh = bytes.fromhex("0500037A0202")  # 8-bit concat ref=0x7A, total=2, seq=2
    part_two = _numeric_deliver_pdu_with_udh(udh, "World")

    first_result = await sms_service.ingest_pdu(part_two)
    await asyncio.sleep(0.01)

    assert first_result is None
    assert await store.count() == 0
    assert received == []

    udh_seq1 = bytes.fromhex("0500037A0201")  # same ref/total, seq=1
    part_one = _numeric_deliver_pdu_with_udh(udh_seq1, "Hello")

    second_result = await sms_service.ingest_pdu(part_one)
    await asyncio.sleep(0.01)

    assert second_result is not None
    assert second_result.sender == "5550123"
    assert second_result.body == "HelloWorld"
    assert await store.count() == 1
    assert len(received) == 1
    assert received[0].body == "HelloWorld"


async def test_ingest_pdu_replay_after_completion_does_not_duplicate_delivery(sms_service, bus, store):
    """A carrier replay of all parts must not create a second logical incoming SMS."""
    received = []

    async def track(event):
        received.append(event)

    bus.subscribe(IncomingSMSEvent, track)
    part_one = _numeric_deliver_pdu_with_udh(bytes.fromhex("0500037A0201"), "Hello")
    part_two = _numeric_deliver_pdu_with_udh(bytes.fromhex("0500037A0202"), "World")

    assert await sms_service.ingest_pdu(part_one) is None
    assert (await sms_service.ingest_pdu(part_two)).body == "HelloWorld"
    assert await sms_service.ingest_pdu(part_one) is None
    assert await sms_service.ingest_pdu(part_two) is None
    await asyncio.sleep(0.01)

    assert await store.count() == 1
    assert [event.body for event in received] == ["HelloWorld"]


def test_pdu_completion_tombstone_expires_after_duplicate_replay(sms_service):
    """A duplicate cannot keep an expired tombstone hidden behind a newer entry."""
    first_info = MultipartInfo(1, 2, 2)
    second_info = MultipartInfo(2, 2, 2)
    sms_service._pdu_completion_max_age = 1.0

    sms_service._record_pdu_completion("first", "body-one", first_info, 0.0)
    sms_service._record_pdu_completion("second", "body-two", second_info, 0.5)

    assert sms_service._has_pdu_completion("first", "body-one", first_info, 0.5)
    assert "first" not in repr(sms_service._recent_pdu_completions)
    assert "body-one" not in repr(sms_service._recent_pdu_completions)
    assert not sms_service._has_pdu_completion("first", "body-one", first_info, 1.1)


async def test_ingest_pdu_reassembles_parts_with_distinct_segment_timestamps(sms_service, bus, store):
    """Concatenated segments retain their shared UDH identity across distinct SCTS values."""
    received = []

    async def track(event):
        received.append(event)

    bus.subscribe(IncomingSMSEvent, track)
    part_one = _numeric_deliver_pdu_with_udh(bytes.fromhex("0500037A0201"), "First")
    part_two = _numeric_deliver_pdu_with_udh(bytes.fromhex("0500037A0202"), "Second")
    part_two = part_two[:20] + "52215241030040" + part_two[34:]

    assert await sms_service.ingest_pdu(part_one) is None
    result = await sms_service.ingest_pdu(part_two)
    await asyncio.sleep(0.01)

    assert result is not None
    assert result.body == "FirstSecond"
    assert await store.count() == 1
    assert [event.body for event in received] == ["FirstSecond"]


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


async def test_messages_stream_exposes_dropped_event_count(sms_service):
    """SMS message consumers can observe bounded-stream overflow safely."""
    async with sms_service.messages(filter_sender="A") as inbox:
        assert inbox.dropped == 0


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
