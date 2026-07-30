"""Tests for the in-memory SMS scheduler kernel."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

import pytest

from callstack.sms.scheduler import ScheduledSMS, SMSScheduler


def make_job(recipient="+1555", body="hi", send_at=None):
    if send_at is None:
        send_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return ScheduledSMS(recipient=recipient, body=body, send_at=send_at)


@dataclass
class FakeResult:
    reference: int


class RecordingSender:
    def __init__(self, result=None, exc=None):
        self.calls = []
        self._result = result
        self._exc = exc

    async def __call__(self, recipient, body):
        self.calls.append((recipient, body))
        if self._exc is not None:
            raise self._exc
        return self._result


def test_scheduled_sms_defaults():
    job = make_job()
    assert job.status == "pending"
    assert job.sent_at is None
    assert job.reference is None
    assert job.last_error is None


async def test_due_job_is_sent_exactly_once():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    job = make_job(send_at=now)
    sender = RecordingSender(result=FakeResult(reference=1))
    scheduler = SMSScheduler(sender=sender, jobs=[job])

    await scheduler.run_due_once(now)

    assert sender.calls == [("+1555", "hi")]
    assert job.status == "sent"
    assert job.sent_at == now
    assert job.reference == 1


async def test_successful_send_without_reference_leaves_reference_none():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    job = make_job(send_at=now)
    sender = RecordingSender(result=None)
    scheduler = SMSScheduler(sender=sender, jobs=[job])

    await scheduler.run_due_once(now)

    assert job.status == "sent"
    assert job.reference is None


async def test_future_job_is_untouched():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    future = now + timedelta(hours=1)
    job = make_job(send_at=future)
    sender = RecordingSender(result=FakeResult(reference=1))
    scheduler = SMSScheduler(sender=sender, jobs=[job])

    await scheduler.run_due_once(now)

    assert sender.calls == []
    assert job.status == "pending"
    assert job.sent_at is None


async def test_sender_failure_marks_only_that_job_failed_and_continues():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    failing_job = make_job(recipient="+1555", send_at=now)
    ok_job = make_job(recipient="+1556", send_at=now)

    class FailingSender:
        def __init__(self):
            self.calls = []

        async def __call__(self, recipient, body):
            self.calls.append((recipient, body))
            if recipient == "+1555":
                raise RuntimeError("boom")
            return FakeResult(reference=2)

    sender = FailingSender()
    scheduler = SMSScheduler(sender=sender, jobs=[failing_job, ok_job])

    await scheduler.run_due_once(now)

    assert failing_job.status == "failed"
    assert failing_job.last_error == "RuntimeError"
    assert "boom" not in (failing_job.last_error or "")
    assert ok_job.status == "sent"
    assert ok_job.reference == 2


async def test_sent_job_is_not_resent_on_later_tick():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    job = make_job(send_at=now)
    sender = RecordingSender(result=FakeResult(reference=1))
    scheduler = SMSScheduler(sender=sender, jobs=[job])

    await scheduler.run_due_once(now)
    later = now + timedelta(hours=1)
    await scheduler.run_due_once(later)

    assert sender.calls == [("+1555", "hi")]


async def test_failed_job_is_not_resent_on_later_tick():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    job = make_job(send_at=now)
    sender = RecordingSender(exc=RuntimeError("boom"))
    scheduler = SMSScheduler(sender=sender, jobs=[job])

    await scheduler.run_due_once(now)
    later = now + timedelta(hours=1)
    await scheduler.run_due_once(later)

    assert sender.calls == [("+1555", "hi")]
    assert job.status == "failed"


async def test_naive_now_raises_value_error():
    job = make_job()
    sender = RecordingSender(result=FakeResult(reference=1))
    scheduler = SMSScheduler(sender=sender, jobs=[job])

    with pytest.raises(ValueError):
        await scheduler.run_due_once(datetime(2026, 1, 1))


async def test_naive_send_at_raises_value_error():
    naive_job = make_job(send_at=datetime(2026, 1, 1))
    sender = RecordingSender(result=FakeResult(reference=1))
    scheduler = SMSScheduler(sender=sender, jobs=[naive_job])

    with pytest.raises(ValueError):
        await scheduler.run_due_once(datetime(2026, 1, 1, tzinfo=timezone.utc))


async def test_invalid_later_send_at_prevents_any_job_dispatch():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    due_job = make_job(recipient="+1555", send_at=now)
    naive_job = make_job(recipient="+1556", send_at=datetime(2026, 1, 1))
    sender = RecordingSender(result=FakeResult(reference=1))
    scheduler = SMSScheduler(sender=sender, jobs=[due_job, naive_job])

    with pytest.raises(ValueError):
        await scheduler.run_due_once(now)

    assert sender.calls == []
    assert due_job.status == "pending"
    assert due_job.sent_at is None
    assert naive_job.status == "pending"
    assert naive_job.sent_at is None


class NoneOffsetTzinfo(tzinfo):
    """tzinfo whose utcoffset() returns None, making it naive per Python semantics."""

    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return None

    def tzname(self, dt):
        return "NONE_OFFSET"


async def test_now_with_none_utcoffset_tzinfo_raises_value_error():
    job = make_job()
    sender = RecordingSender(result=FakeResult(reference=1))
    scheduler = SMSScheduler(sender=sender, jobs=[job])

    now = datetime(2026, 1, 1, tzinfo=NoneOffsetTzinfo())

    with pytest.raises(ValueError):
        await scheduler.run_due_once(now)


async def test_send_at_with_none_utcoffset_tzinfo_raises_value_error():
    bad_job = make_job(send_at=datetime(2026, 1, 1, tzinfo=NoneOffsetTzinfo()))
    sender = RecordingSender(result=FakeResult(reference=1))
    scheduler = SMSScheduler(sender=sender, jobs=[bad_job])

    with pytest.raises(ValueError):
        await scheduler.run_due_once(datetime(2026, 1, 1, tzinfo=timezone.utc))


async def test_past_due_job_is_sent():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    past = now - timedelta(hours=1)
    job = make_job(send_at=past)
    sender = RecordingSender(result=FakeResult(reference=1))
    scheduler = SMSScheduler(sender=sender, jobs=[job])

    await scheduler.run_due_once(now)

    assert sender.calls == [("+1555", "hi")]
    assert job.status == "sent"
    assert job.sent_at == now
    assert job.reference == 1


async def test_dst_fall_back_fold_normalizes_to_utc_before_due_comparison():
    tz = ZoneInfo("America/New_York")
    # 2026-11-01 01:30 America/New_York occurs twice (DST fall-back).
    # fold=1 is the second occurrence, UTC 06:30.
    send_at = datetime(2026, 11, 1, 1, 30, tzinfo=tz, fold=1)
    job = make_job(send_at=send_at)
    sender = RecordingSender(result=FakeResult(reference=1))
    scheduler = SMSScheduler(sender=sender, jobs=[job])

    # fold=0 is the first occurrence of 01:45, UTC 05:45 - earlier than send_at.
    now_fold0 = datetime(2026, 11, 1, 1, 45, tzinfo=tz, fold=0)
    await scheduler.run_due_once(now_fold0)

    assert sender.calls == []
    assert job.status == "pending"

    # fold=1 is the second occurrence of 01:45, UTC 06:45 - at/after send_at.
    now_fold1 = datetime(2026, 11, 1, 1, 45, tzinfo=tz, fold=1)
    await scheduler.run_due_once(now_fold1)

    assert sender.calls == [("+1555", "hi")]
    assert job.status == "sent"


async def test_overlapping_run_due_once_sends_due_job_only_once():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    job = make_job(send_at=now)

    release = asyncio.Event()
    calls = []

    async def blocking_sender(recipient, body):
        calls.append((recipient, body))
        await release.wait()
        return FakeResult(reference=1)

    scheduler = SMSScheduler(sender=blocking_sender, jobs=[job])

    task1 = asyncio.create_task(scheduler.run_due_once(now))
    task2 = asyncio.create_task(scheduler.run_due_once(now))

    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(calls) == 1

    release.set()
    await asyncio.gather(task1, task2)

    assert len(calls) == 1
    assert job.status == "sent"
