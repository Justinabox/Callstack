"""Tests for SMS store."""

import asyncio

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


async def test_sqlite_initialize_is_idempotent(tmp_path):
    pytest.importorskip("aiosqlite")
    store = SMSStore(db_path=str(tmp_path / "sms.db"))
    try:
        await store.initialize()
        db = store._db
        assert db is not None
        saved = await store.save(SMS(body="hello"))

        await store.initialize()

        assert store._db is db
        assert await store.count() == 1
        messages = await store.list()
        assert len(messages) == 1
        assert messages[0].id == saved.id
        assert messages[0].body == "hello"
    finally:
        await store.close()


async def test_sqlite_initialize_serializes_concurrent_calls(tmp_path, monkeypatch):
    aiosqlite = pytest.importorskip("aiosqlite")
    original_connect = aiosqlite.connect
    connect_count = 0

    async def slow_connect(*args, **kwargs):
        nonlocal connect_count
        connect_count += 1
        await asyncio.sleep(0)
        return await original_connect(*args, **kwargs)

    monkeypatch.setattr(aiosqlite, "connect", slow_connect)

    store = SMSStore(db_path=str(tmp_path / "sms.db"))
    try:
        await asyncio.gather(store.initialize(), store.initialize())
        assert connect_count == 1
        assert store._db is not None
    finally:
        await store.close()


async def test_sqlite_readonly_initialize_does_not_create_missing_database(tmp_path):
    pytest.importorskip("aiosqlite")
    db_path = tmp_path / "missing.db"
    store = SMSStore(db_path=str(db_path))

    with pytest.raises(FileNotFoundError):
        await store.initialize(readonly=True)

    assert not db_path.exists()


async def test_sqlite_readonly_initialize_does_not_create_messages_table_in_unrelated_database(tmp_path):
    aiosqlite = pytest.importorskip("aiosqlite")
    db_path = tmp_path / "unrelated.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
        await db.commit()

    store = SMSStore(db_path=str(db_path))
    with pytest.raises(Exception, match="messages"):
        await store.initialize(readonly=True)

    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
        ) as cursor:
            assert await cursor.fetchone() is None


async def test_sqlite_readonly_initialize_escapes_uri_reserved_path_characters(tmp_path):
    pytest.importorskip("aiosqlite")
    db_path = tmp_path / "has?question.db"
    store = SMSStore(db_path=str(db_path))
    try:
        await store.initialize()
        await store.save(SMS(body="kept"))
    finally:
        await store.close()

    readonly_store = SMSStore(db_path=str(db_path))
    try:
        await readonly_store.initialize(readonly=True)
        messages = await readonly_store.list()
    finally:
        await readonly_store.close()

    assert [message.body for message in messages] == ["kept"]
    assert not (tmp_path / "has").exists()
