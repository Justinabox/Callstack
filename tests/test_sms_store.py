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


async def test_sqlite_close_then_initialize_reloads_without_duplicate_rows(tmp_path):
    pytest.importorskip("aiosqlite")
    store = SMSStore(db_path=str(tmp_path / "sms.db"))
    try:
        await store.initialize()
        saved = await store.save(SMS(body="hello", status="unread"))
        assert saved.id == 1
        await store.close()

        await store.initialize()

        assert await store.count() == 1
        messages = await store.list()
        assert [(m.id, m.body, m.status) for m in messages] == [(saved.id, "hello", "unread")]
        next_saved = await store.save(SMS(body="after reopen"))
        assert next_saved.id == 2
    finally:
        await store.close()


async def test_sqlite_initialize_preserves_save_before_first_initialize(tmp_path):
    pytest.importorskip("aiosqlite")
    store = SMSStore(db_path=str(tmp_path / "sms.db"))
    try:
        pending = await store.save(SMS(body="queued before init"))

        await store.initialize()

        assert await store.count() == 1
        messages = await store.list()
        assert [(m.id, m.body) for m in messages] == [(pending.id, "queued before init")]
        assert (await store.save(SMS(body="after init"))).id == 2
        await store.close()
        await store.initialize()
        assert [(m.id, m.body) for m in await store.list()] == [
            (pending.id, "queued before init"),
            (2, "after init"),
        ]
    finally:
        await store.close()


async def test_sqlite_initialize_preserves_save_after_close(tmp_path):
    pytest.importorskip("aiosqlite")
    store = SMSStore(db_path=str(tmp_path / "sms.db"))
    try:
        await store.initialize()
        persisted = await store.save(SMS(body="persisted"))
        await store.close()
        pending = await store.save(SMS(body="queued while closed"))

        await store.initialize()

        assert await store.count() == 2
        messages = await store.list()
        assert [(m.id, m.body) for m in messages] == [
            (persisted.id, "persisted"),
            (pending.id, "queued while closed"),
        ]
        assert (await store.save(SMS(body="after reopen"))).id == 3
        await store.close()
        await store.initialize()
        assert [(m.id, m.body) for m in await store.list()] == [
            (persisted.id, "persisted"),
            (pending.id, "queued while closed"),
            (3, "after reopen"),
        ]
    finally:
        await store.close()


async def test_sqlite_initialize_persists_update_saved_after_close(tmp_path):
    pytest.importorskip("aiosqlite")
    store = SMSStore(db_path=str(tmp_path / "sms.db"))
    try:
        await store.initialize()
        persisted = await store.save(SMS(body="old", status="unread"))
        await store.close()
        await store.save(SMS(id=persisted.id, body="updated", status="read"))

        await store.initialize()

        assert [(m.id, m.body, m.status) for m in await store.list()] == [
            (persisted.id, "updated", "read")
        ]
        await store.close()
        await store.initialize()
        assert [(m.id, m.body, m.status) for m in await store.list()] == [
            (persisted.id, "updated", "read")
        ]
    finally:
        await store.close()


async def test_sqlite_initialize_reassigns_pending_insert_id_collision(tmp_path):
    pytest.importorskip("aiosqlite")
    db_path = str(tmp_path / "sms.db")
    existing = SMSStore(db_path=db_path)
    try:
        await existing.initialize()
        persisted = await existing.save(SMS(body="persisted elsewhere"))
    finally:
        await existing.close()

    store = SMSStore(db_path=db_path)
    try:
        pending = await store.save(SMS(body="queued before init"))
        assert pending.id == persisted.id

        await store.initialize()

        assert pending.id == 2
        assert [(m.id, m.body) for m in await store.list()] == [
            (persisted.id, "persisted elsewhere"),
            (pending.id, "queued before init"),
        ]
        await store.close()
        await store.initialize()
        assert [(m.id, m.body) for m in await store.list()] == [
            (persisted.id, "persisted elsewhere"),
            (pending.id, "queued before init"),
        ]
    finally:
        await store.close()


async def test_sqlite_initialize_preserves_auto_insert_after_pending_update_collision(tmp_path):
    pytest.importorskip("aiosqlite")
    db_path = str(tmp_path / "sms.db")
    existing = SMSStore(db_path=db_path)
    try:
        await existing.initialize()
        persisted = await existing.save(SMS(body="persisted elsewhere"))
    finally:
        await existing.close()

    store = SMSStore(db_path=db_path)
    try:
        pending = await store.save(SMS(body="queued before init"))
        updated = await store.save(SMS(id=pending.id, body="queued updated before init"))

        await store.initialize()

        assert updated.id == 2
        assert [(m.id, m.body) for m in await store.list()] == [
            (persisted.id, "persisted elsewhere"),
            (updated.id, "queued updated before init"),
        ]
    finally:
        await store.close()


async def test_sqlite_initialize_reassigns_auto_collision_around_pending_explicit_id(tmp_path):
    pytest.importorskip("aiosqlite")
    db_path = str(tmp_path / "sms.db")
    existing = SMSStore(db_path=db_path)
    try:
        await existing.initialize()
        persisted = await existing.save(SMS(body="persisted id 1"))
    finally:
        await existing.close()

    store = SMSStore(db_path=db_path)
    try:
        auto_pending = await store.save(SMS(body="auto pending"))
        explicit_pending = await store.save(SMS(id=2, body="explicit pending"))

        await store.initialize()

        assert auto_pending.id == 3
        assert [(m.id, m.body) for m in await store.list()] == [
            (persisted.id, "persisted id 1"),
            (explicit_pending.id, "explicit pending"),
            (auto_pending.id, "auto pending"),
        ]
    finally:
        await store.close()


async def test_sqlite_delete_removes_pending_save_before_initialize(tmp_path):
    pytest.importorskip("aiosqlite")
    store = SMSStore(db_path=str(tmp_path / "sms.db"))
    try:
        pending = await store.save(SMS(body="delete before init"))
        assert pending.id is not None
        assert await store.delete(pending.id)

        await store.initialize()

        assert await store.count() == 0
    finally:
        await store.close()


async def test_sqlite_clear_removes_pending_saves_before_initialize(tmp_path):
    pytest.importorskip("aiosqlite")
    store = SMSStore(db_path=str(tmp_path / "sms.db"))
    try:
        await store.save(SMS(body="clear before init"))
        await store.clear()

        await store.initialize()

        assert await store.count() == 0
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
