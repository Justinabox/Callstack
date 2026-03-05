"""Tests for SMS store."""

import pytest
from callstack.sms.store import SMSStore
from callstack.sms.types import SMS


@pytest.fixture
def store():
    return SMSStore()


async def test_save_assigns_id(store):
    sms = SMS(sender="+1555", body="hello")
    result = await store.save(sms)
    assert result.id == 1


async def test_save_increments_ids(store):
    await store.save(SMS(body="one"))
    sms2 = await store.save(SMS(body="two"))
    assert sms2.id == 2


async def test_get_by_id(store):
    await store.save(SMS(body="hello"))
    result = await store.get(1)
    assert result is not None
    assert result.body == "hello"


async def test_get_missing(store):
    result = await store.get(999)
    assert result is None


async def test_list_all(store):
    await store.save(SMS(sender="A", body="1"))
    await store.save(SMS(sender="B", body="2"))
    all_msgs = await store.list()
    assert len(all_msgs) == 2


async def test_list_filter_sender(store):
    await store.save(SMS(sender="A", body="1"))
    await store.save(SMS(sender="B", body="2"))
    await store.save(SMS(sender="A", body="3"))
    results = await store.list(sender="A")
    assert len(results) == 2
    assert all(m.sender == "A" for m in results)


async def test_list_filter_status(store):
    await store.save(SMS(status="sent", body="1"))
    await store.save(SMS(status="unread", body="2"))
    results = await store.list(status="sent")
    assert len(results) == 1


async def test_list_limit(store):
    for i in range(10):
        await store.save(SMS(body=str(i)))
    results = await store.list(limit=3)
    assert len(results) == 3
    # Should return the last 3
    assert results[0].body == "7"


async def test_delete(store):
    await store.save(SMS(body="delete me"))
    assert await store.delete(1)
    assert await store.get(1) is None


async def test_delete_missing(store):
    assert not await store.delete(999)


async def test_count(store):
    assert await store.count() == 0
    await store.save(SMS(body="one"))
    assert await store.count() == 1


async def test_clear(store):
    await store.save(SMS(body="one"))
    await store.save(SMS(body="two"))
    await store.clear()
    assert await store.count() == 0
