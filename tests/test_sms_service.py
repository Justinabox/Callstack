"""Tests for the SMS service."""

import asyncio
import pytest
from callstack.events.bus import EventBus
from callstack.events.types import IncomingSMSEvent, SMSSentEvent, _RawSMSNotification
from callstack.errors import SMSSendError
from callstack.protocol.executor import ATCommandExecutor
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
    assert any("CNMI" in w for w in written)


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
        await sms_service.send("+15551234", "Hello!")


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
    transport.feed('+CMGR: "REC UNREAD","+15559876","","24/12/25,14:30:00+04"', "Hello there!", "OK")
    transport.feed("OK")  # Response for AT+CMGD (delete after read)

    # Simulate URC dispatch (now uses _RawSMSNotification)
    await bus.emit(_RawSMSNotification(raw='+CMTI: "SM",3'))

    await asyncio.sleep(0.05)
    # The re-emitted enriched event (empty raw, populated sender/body)
    enriched = [e for e in all_events if e.sender == "+15559876" and not e.raw]
    assert len(enriched) == 1
    assert enriched[0].body == "Hello there!"


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


# -- Message Management --

async def test_list_messages(sms_service, transport):
    """List messages from SIM."""
    transport.feed(
        '+CMGL: 0,"REC UNREAD","+15551111","","24/12/25,10:00:00+04"',
        "Hello",
        '+CMGL: 1,"REC READ","+15552222","","24/12/25,11:00:00+04"',
        "World",
        "OK",
    )
    messages = await sms_service.list_messages()
    assert len(messages) == 2
    assert messages[0].sender == "+15551111"
    assert messages[0].body == "Hello"
    assert messages[0].storage_index == 0
    assert messages[1].sender == "+15552222"
    assert messages[1].body == "World"


async def test_list_messages_empty(sms_service, transport):
    """List messages when SIM is empty."""
    transport.feed("OK")
    messages = await sms_service.list_messages()
    assert messages == []


async def test_read_message(sms_service, transport):
    """Read a single message."""
    transport.feed(
        '+CMGR: "REC UNREAD","+15551234","","24/12/25,14:30:00+04"',
        "Test body",
        "OK",
    )
    sms = await sms_service.read_message(0)
    assert sms is not None
    assert sms.sender == "+15551234"
    assert sms.body == "Test body"
    assert sms.storage_index == 0


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

    assert _parse_timestamp("") is None
    assert _parse_timestamp("invalid") is None
