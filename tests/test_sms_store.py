"""Tests for SMS store."""

import asyncio

import pytest

import callstack.sms.store as sms_store_module
from callstack.sms.store import SMSStore
from callstack.sms.types import DeliveryReport, SMS


@pytest.fixture
def store():
    return SMSStore()


async def test_save_delivery_report_correlates_unique_matching_outbound_sms(store):
    match = await store.save(SMS(recipient="+15551234567", status="sent", reference=42))
    await store.save(SMS(recipient="+15557654321", status="sent", reference=42))

    report = await store.save_delivery_report(
        DeliveryReport(reference=42, recipient="+15551234567", status="delivered")
    )

    assert report.id == 1
    assert report.message_id == match.id
    assert (await store.get(match.id)).status == "delivered"
    listed = await store.list_delivery_reports()
    assert [(item.id, item.reference, item.message_id, item.status) for item in listed] == [
        (1, 42, match.id, "delivered")
    ]


async def test_save_delivery_report_does_not_correlate_when_multiple_outbound_matches(store):
    first = await store.save(SMS(recipient="+15551234567", status="sent", reference=42))
    second = await store.save(SMS(recipient="+15551234567", status="sent", reference=42))

    report = await store.save_delivery_report(
        DeliveryReport(reference=42, recipient="+15551234567", status="delivered")
    )

    assert report.message_id is None
    assert (await store.get(first.id)).status == "sent"
    assert (await store.get(second.id)).status == "sent"


async def test_sqlite_delivery_report_ambiguous_match_stays_unresolved_after_reopen(tmp_path):
    pytest.importorskip("aiosqlite")
    db_path = str(tmp_path / "sms.db")
    store = SMSStore(db_path=db_path)
    try:
        await store.initialize()
        first = await store.save(SMS(recipient="+15551234567", status="sent", reference=46))
        second = await store.save(SMS(recipient="+15551234567", status="sent", reference=46))
        saved = await store.save_delivery_report(
            DeliveryReport(reference=46, recipient="+15551234567", status="delivered")
        )
        assert saved.message_id is None
        await store.close()

        reopened = SMSStore(db_path=db_path)
        try:
            await reopened.initialize()
            reports = await reopened.list_delivery_reports()
            messages = await reopened.list()
        finally:
            await reopened.close()

        assert [
            (report.reference, report.recipient, report.status, report.message_id)
            for report in reports
        ] == [(46, "+15551234567", "delivered", None)]
        assert [(message.id, message.status) for message in messages] == [
            (first.id, "sent"),
            (second.id, "sent"),
        ]
    finally:
        await store.close()


async def test_sqlite_delivery_reports_survive_reopen_and_preserve_message_status(tmp_path):
    pytest.importorskip("aiosqlite")
    db_path = str(tmp_path / "sms.db")
    store = SMSStore(db_path=db_path)
    try:
        await store.initialize()
        sms = await store.save(SMS(recipient="+15551234567", status="sent", reference=43))
        saved = await store.save_delivery_report(
            DeliveryReport(reference=43, recipient="+15551234567", status="failed")
        )
        assert saved.message_id == sms.id
        await store.close()

        reopened = SMSStore(db_path=db_path)
        try:
            await reopened.initialize()
            reports = await reopened.list_delivery_reports()
            messages = await reopened.list()
        finally:
            await reopened.close()

        assert [(report.reference, report.recipient, report.status, report.message_id) for report in reports] == [
            (43, "+15551234567", "failed", sms.id)
        ]
        assert [(message.id, message.status) for message in messages] == [(sms.id, "failed")]
    finally:
        await store.close()


async def test_save_delivery_report_does_not_correlate_inbound_message_with_same_reference(store):
    incoming = await store.save(SMS(sender="+155****0000", status="unread", reference=42))

    report = await store.save_delivery_report(
        DeliveryReport(reference=42, recipient="+155****4567", status="delivered")
    )

    assert report.message_id is None
    assert (await store.get(incoming.id)).status == "unread"


async def test_save_delivery_report_ignores_inbound_message_sharing_recipient_and_reference(store):
    incoming = await store.save(
        SMS(recipient="+15551234567", status="unread", reference=42)
    )
    outbound = await store.save(
        SMS(recipient="+15551234567", status="sent", reference=42)
    )

    report = await store.save_delivery_report(
        DeliveryReport(reference=42, recipient="+15551234567", status="delivered")
    )

    assert report.message_id == outbound.id
    assert (await store.get(outbound.id)).status == "delivered"
    assert (await store.get(incoming.id)).status == "unread"


async def test_delivery_report_saved_before_initialize_persists_on_initialize(tmp_path):
    pytest.importorskip("aiosqlite")
    store = SMSStore(db_path=str(tmp_path / "sms.db"))
    try:
        saved = await store.save_delivery_report(
            DeliveryReport(reference=44, recipient="+155****4567", status="delivered")
        )

        await store.initialize()

        assert [(report.id, report.reference, report.recipient, report.status) for report in await store.list_delivery_reports()] == [
            (saved.id, 44, "+155****4567", "delivered")
        ]
        await store.close()
        await store.initialize()
        assert [(report.id, report.reference, report.recipient, report.status) for report in await store.list_delivery_reports()] == [
            (saved.id, 44, "+155****4567", "delivered")
        ]
    finally:
        await store.close()


async def test_sqlite_save_delivery_report_replaces_existing_report_id(tmp_path):
    pytest.importorskip("aiosqlite")
    store = SMSStore(db_path=str(tmp_path / "sms.db"))
    try:
        await store.initialize()
        report = await store.save_delivery_report(
            DeliveryReport(reference=45, recipient="+155****4567", status="pending")
        )
        report.status = "delivered"

        await store.save_delivery_report(report)

        assert [(item.id, item.status) for item in await store.list_delivery_reports()] == [
            (report.id, "delivered")
        ]
        await store.close()
        await store.initialize()
        assert [(item.id, item.status) for item in await store.list_delivery_reports()] == [
            (report.id, "delivered")
        ]
    finally:
        await store.close()


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


@pytest.mark.parametrize("limit", [0, -1, False, True])
async def test_list_rejects_non_positive_and_bool_limits(store, limit):
    await store.save(SMS(body="one"))
    await store.save(SMS(body="two"))

    with pytest.raises(ValueError):
        await store.list(limit=limit)


@pytest.mark.parametrize("limit", [0, -1, False, True])
async def test_list_delivery_reports_rejects_non_positive_and_bool_limits(store, limit):
    await store.save_delivery_report(DeliveryReport(reference=1, status="delivered"))
    await store.save_delivery_report(DeliveryReport(reference=2, status="failed"))

    with pytest.raises(ValueError):
        await store.list_delivery_reports(limit=limit)


async def test_sqlite_list_limits_preserve_newest_last_order(tmp_path):
    pytest.importorskip("aiosqlite")
    store = SMSStore(db_path=str(tmp_path / "sms.db"))
    try:
        await store.initialize()
        for i in range(5):
            await store.save(SMS(body=str(i)))
        await store.save_delivery_report(DeliveryReport(reference=1, status="pending"))
        await store.save_delivery_report(DeliveryReport(reference=2, status="delivered"))
        await store.save_delivery_report(DeliveryReport(reference=3, status="failed"))

        messages = await store.list(limit=2)
        reports = await store.list_delivery_reports(limit=2)
    finally:
        await store.close()

    assert [message.body for message in messages] == ["3", "4"]
    assert [(report.reference, report.status) for report in reports] == [
        (2, "delivered"),
        (3, "failed"),
    ]


async def test_list_methods_accept_positive_int_subclass_limits(store):
    class StoreLimit(int):
        pass

    for i in range(3):
        await store.save(SMS(body=str(i)))
    await store.save_delivery_report(DeliveryReport(reference=1, status="pending"))
    await store.save_delivery_report(DeliveryReport(reference=2, status="delivered"))

    messages = await store.list(limit=StoreLimit(2))
    reports = await store.list_delivery_reports(limit=StoreLimit(1))

    assert [message.body for message in messages] == ["1", "2"]
    assert [(report.reference, report.status) for report in reports] == [
        (2, "delivered")
    ]


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


async def test_clear_also_clears_delivery_reports(store):
    await store.save(SMS(body="one"))
    await store.save_delivery_report(DeliveryReport(reference=1, status="delivered"))

    await store.clear()

    assert await store.count() == 0
    assert await store.list_delivery_reports() == []


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


async def test_sqlite_delete_after_close_deletes_persisted_row(tmp_path):
    pytest.importorskip("aiosqlite")
    store = SMSStore(db_path=str(tmp_path / "sms.db"))
    try:
        await store.initialize()
        saved = await store.save(SMS(body="persisted before delete", status="unread"))
        assert saved.id is not None
        await store.close()

        assert await store.delete(saved.id)

        assert store._db is None
        assert await store.count() == 0
        await store.initialize()
        assert await store.get(saved.id) is None
        assert await store.list() == []
    finally:
        await store.close()


async def test_sqlite_delete_after_close_preserves_message_when_durable_delete_fails(tmp_path, monkeypatch):
    aiosqlite = pytest.importorskip("aiosqlite")
    store = SMSStore(db_path=str(tmp_path / "sms.db"))
    try:
        await store.initialize()
        saved = await store.save(SMS(body="keep retryable", status="unread"))
        assert saved.id is not None
        await store.close()

        class FailingConnection:
            async def execute(self, *_args, **_kwargs):
                raise OSError("database locked")

            async def commit(self):
                raise AssertionError("commit should not run after failed delete")

            async def close(self):
                pass

        async def failing_connect(*_args, **_kwargs):
            return FailingConnection()

        monkeypatch.setattr(aiosqlite, "connect", failing_connect)

        with pytest.raises(OSError, match="database locked"):
            await store.delete(saved.id)

        assert await store.count() == 1
        assert await store.get(saved.id) is saved
    finally:
        await store.close()


async def test_sqlite_delete_after_close_fails_closed_without_aiosqlite(tmp_path, monkeypatch):
    pytest.importorskip("aiosqlite")
    store = SMSStore(db_path=str(tmp_path / "sms.db"))
    try:
        await store.initialize()
        saved = await store.save(SMS(body="requires durable delete"))
        assert saved.id is not None
        await store.close()

        real_import_module = sms_store_module.importlib.import_module

        def import_without_aiosqlite(name, *args, **kwargs):
            if name == "aiosqlite":
                raise ImportError("aiosqlite unavailable")
            return real_import_module(name, *args, **kwargs)

        monkeypatch.setattr(sms_store_module.importlib, "import_module", import_without_aiosqlite)

        assert await store.delete(saved.id) is False
        assert await store.count() == 1
        assert await store.get(saved.id) is saved
    finally:
        await store.close()


async def test_sqlite_delete_auto_pending_collision_keeps_existing_row(tmp_path):
    pytest.importorskip("aiosqlite")
    db_path = str(tmp_path / "sms.db")
    existing = SMSStore(db_path=db_path)
    try:
        await existing.initialize()
        persisted = await existing.save(SMS(body="persisted elsewhere"))
        assert persisted.id == 1
    finally:
        await existing.close()

    store = SMSStore(db_path=db_path)
    try:
        pending = await store.save(SMS(body="pending before init"))
        assert pending.id == persisted.id

        assert await store.delete(pending.id)
        assert await store.count() == 0

        await store.initialize()
        assert [(sms.id, sms.body) for sms in await store.list()] == [
            (persisted.id, "persisted elsewhere")
        ]
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


async def test_sqlite_clear_before_first_initialize_deletes_existing_rows(tmp_path):
    pytest.importorskip("aiosqlite")
    db_path = str(tmp_path / "sms.db")
    existing = SMSStore(db_path=db_path)
    try:
        await existing.initialize()
        await existing.save(SMS(body="persisted by previous store"))
    finally:
        await existing.close()

    store = SMSStore(db_path=db_path)
    try:
        await store.clear()

        assert store._db is None
        await store.initialize()
        assert await store.count() == 0
        assert await store.list() == []
    finally:
        await store.close()


async def test_sqlite_clear_while_open_remains_durable(tmp_path):
    pytest.importorskip("aiosqlite")
    store = SMSStore(db_path=str(tmp_path / "sms.db"))
    try:
        await store.initialize()
        await store.save(SMS(body="persisted while open"))

        await store.clear()
        await store.close()
        await store.initialize()

        assert await store.count() == 0
        assert await store.list() == []
    finally:
        await store.close()


async def test_sqlite_clear_after_close_deletes_persisted_rows(tmp_path):
    pytest.importorskip("aiosqlite")
    store = SMSStore(db_path=str(tmp_path / "sms.db"))
    try:
        await store.initialize()
        await store.save(SMS(body="persisted before close"))
        await store.close()

        await store.clear()

        assert store._db is None
        assert await store.count() == 0
        await store.initialize()
        assert await store.count() == 0
        assert await store.list() == []
    finally:
        await store.close()


async def test_sqlite_clear_after_close_discards_pending_saves(tmp_path):
    pytest.importorskip("aiosqlite")
    store = SMSStore(db_path=str(tmp_path / "sms.db"))
    try:
        await store.initialize()
        await store.save(SMS(body="persisted before close"))
        await store.close()
        await store.save(SMS(body="queued while closed"))

        await store.clear()
        await store.initialize()

        assert await store.count() == 0
        assert await store.list() == []
    finally:
        await store.close()


async def test_sqlite_clear_while_open_also_clears_delivery_reports(tmp_path):
    pytest.importorskip("aiosqlite")
    store = SMSStore(db_path=str(tmp_path / "sms.db"))
    try:
        await store.initialize()
        await store.save(SMS(body="persisted while open"))
        await store.save_delivery_report(DeliveryReport(reference=1, status="delivered"))

        await store.clear()
        await store.close()
        await store.initialize()

        assert await store.count() == 0
        assert await store.list_delivery_reports() == []
    finally:
        await store.close()


async def test_sqlite_clear_after_close_deletes_persisted_and_pending_delivery_reports(tmp_path):
    pytest.importorskip("aiosqlite")
    store = SMSStore(db_path=str(tmp_path / "sms.db"))
    try:
        await store.initialize()
        await store.save_delivery_report(DeliveryReport(reference=1, status="delivered"))
        await store.close()
        await store.save_delivery_report(DeliveryReport(reference=2, status="failed"))

        await store.clear()
        await store.initialize()

        assert await store.list_delivery_reports() == []
    finally:
        await store.close()


async def test_sqlite_clear_before_first_initialize_discards_pending_delivery_report(tmp_path):
    pytest.importorskip("aiosqlite")
    store = SMSStore(db_path=str(tmp_path / "sms.db"))
    try:
        await store.save_delivery_report(DeliveryReport(reference=1, status="delivered"))

        await store.clear()
        await store.initialize()

        assert await store.list_delivery_reports() == []
    finally:
        await store.close()


async def test_sqlite_clear_while_open_preserves_state_when_durable_delete_fails(tmp_path, monkeypatch):
    pytest.importorskip("aiosqlite")
    store = SMSStore(db_path=str(tmp_path / "sms.db"))
    try:
        await store.initialize()
        saved = await store.save(SMS(body="keep on failed clear", status="unread"))
        report = await store.save_delivery_report(
            DeliveryReport(reference=1, status="delivered")
        )

        real_execute = store._db.execute

        async def failing_execute(sql, *args, **kwargs):
            if sql.strip().upper().startswith("DELETE"):
                raise OSError("database locked")
            return await real_execute(sql, *args, **kwargs)

        monkeypatch.setattr(store._db, "execute", failing_execute)

        with pytest.raises(OSError, match="database locked"):
            await store.clear()

        assert await store.count() == 1
        assert await store.get(saved.id) is saved
        assert await store.list_delivery_reports() == [report]
    finally:
        await store.close()


async def test_sqlite_clear_while_open_rolls_back_and_preserves_state_on_cancellation(tmp_path, monkeypatch):
    pytest.importorskip("aiosqlite")
    store = SMSStore(db_path=str(tmp_path / "sms.db"))
    try:
        await store.initialize()
        saved = await store.save(SMS(body="keep on cancelled clear", status="unread"))
        report = await store.save_delivery_report(
            DeliveryReport(reference=1, status="delivered")
        )

        real_execute = store._db.execute
        real_rollback = store._db.rollback
        rollback_calls = []

        async def cancelling_execute(sql, *args, **kwargs):
            if sql.strip().upper().startswith("DELETE"):
                raise asyncio.CancelledError()
            return await real_execute(sql, *args, **kwargs)

        async def tracking_rollback(*args, **kwargs):
            rollback_calls.append(True)
            return await real_rollback(*args, **kwargs)

        monkeypatch.setattr(store._db, "execute", cancelling_execute)
        monkeypatch.setattr(store._db, "rollback", tracking_rollback)

        with pytest.raises(asyncio.CancelledError):
            await store.clear()

        assert rollback_calls == [True]
        assert await store.count() == 1
        assert await store.get(saved.id) is saved
        assert await store.list_delivery_reports() == [report]
    finally:
        await store.close()


async def test_sqlite_clear_while_open_clears_memory_when_commit_then_cancels(tmp_path, monkeypatch):
    pytest.importorskip("aiosqlite")
    store = SMSStore(db_path=str(tmp_path / "sms.db"))
    try:
        await store.initialize()
        await store.save(SMS(body="erased before cancellation", status="unread"))
        await store.save_delivery_report(DeliveryReport(reference=1, status="delivered"))

        real_commit = store._db.commit

        async def commit_then_cancel():
            await real_commit()
            raise asyncio.CancelledError()

        monkeypatch.setattr(store._db, "commit", commit_then_cancel)

        with pytest.raises(asyncio.CancelledError):
            await store.clear()

        assert store._messages == []
        assert store._delivery_reports == []
    finally:
        await store.close()


async def test_sqlite_clear_after_close_rolls_back_before_closing_on_failure(tmp_path, monkeypatch):
    aiosqlite = pytest.importorskip("aiosqlite")
    store = SMSStore(db_path=str(tmp_path / "sms.db"))
    try:
        await store.initialize()
        saved = await store.save(SMS(body="keep on failed closed clear", status="unread"))
        report = await store.save_delivery_report(
            DeliveryReport(reference=1, status="delivered")
        )
        await store.close()

        calls = []

        class FailingConnection:
            async def execute(self, sql, *_args, **_kwargs):
                if sql.strip().upper().startswith("DELETE"):
                    raise OSError("database locked")
                return None

            async def rollback(self):
                calls.append("rollback")

            async def commit(self):
                raise AssertionError("commit should not run after failed delete")

            async def close(self):
                calls.append("close")

        async def failing_connect(*_args, **_kwargs):
            return FailingConnection()

        monkeypatch.setattr(aiosqlite, "connect", failing_connect)

        with pytest.raises(OSError, match="database locked"):
            await store.clear()

        assert calls == ["rollback", "close"]
        assert await store.count() == 1
        assert await store.get(saved.id) is saved
        assert await store.list_delivery_reports() == [report]
    finally:
        await store.close()


async def test_sqlite_clear_after_close_fails_closed_without_aiosqlite(tmp_path, monkeypatch):
    pytest.importorskip("aiosqlite")
    store = SMSStore(db_path=str(tmp_path / "sms.db"))
    try:
        await store.initialize()
        saved = await store.save(SMS(body="keep without aiosqlite", status="unread"))
        report = await store.save_delivery_report(
            DeliveryReport(reference=1, status="delivered")
        )
        await store.close()

        real_import_module = sms_store_module.importlib.import_module

        def import_without_aiosqlite(name, *args, **kwargs):
            if name == "aiosqlite":
                raise ImportError("aiosqlite unavailable")
            return real_import_module(name, *args, **kwargs)

        monkeypatch.setattr(sms_store_module.importlib, "import_module", import_without_aiosqlite)

        with pytest.raises(RuntimeError, match="aiosqlite"):
            await store.clear()

        assert await store.count() == 1
        assert await store.get(saved.id) is saved
        assert await store.list_delivery_reports() == [report]
    finally:
        await store.close()


async def test_sqlite_clear_after_close_clears_memory_when_close_fails_after_commit(tmp_path, monkeypatch):
    aiosqlite = pytest.importorskip("aiosqlite")
    store = SMSStore(db_path=str(tmp_path / "sms.db"))
    try:
        await store.initialize()
        await store.save(SMS(body="erased durably before close fails", status="unread"))
        await store.save_delivery_report(DeliveryReport(reference=1, status="delivered"))
        await store.close()

        class ClosingFailsConnection:
            async def execute(self, *_args, **_kwargs):
                return None

            async def commit(self):
                return None

            async def close(self):
                raise OSError("close failed")

        async def closing_fails_connect(*_args, **_kwargs):
            return ClosingFailsConnection()

        monkeypatch.setattr(aiosqlite, "connect", closing_fails_connect)

        with pytest.raises(OSError, match="close failed"):
            await store.clear()

        assert store._messages == []
        assert store._delivery_reports == []
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
