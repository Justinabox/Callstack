"""Tests for bounded PII-aware call history records."""

from datetime import datetime, timezone

import pytest

from callstack.voice.history import CallHistoryRecorder, render_call_record


def _at(seconds: int) -> datetime:
    return datetime(2026, 1, 1, 12, 0, seconds, tzinfo=timezone.utc)


def test_recorder_tracks_inbound_answer_and_remote_hangup_with_redacted_rendering():
    recorder = CallHistoryRecorder(max_records=10)

    call_id = recorder.start_call(
        direction="inbound",
        caller="+15551230100",
        started_at=_at(0),
    )
    recorder.mark_answered(call_id, answered_at=_at(3))
    record = recorder.finalize(
        call_id,
        status="ended",
        ended_at=_at(10),
        termination_reason="remote_hangup",
    )

    assert record.direction == "inbound"
    assert record.status == "ended"
    assert record.started_at == _at(0)
    assert record.answered_at == _at(3)
    assert record.ended_at == _at(10)
    assert record.duration_seconds == 7.0
    assert record.caller == "+15551230100"

    public = render_call_record(record, redact=True)
    assert public["caller"] == "+***0100"
    assert "+15551230100" not in str(public)


def test_recorder_bounds_records_and_returns_newest_first_with_limit_validation():
    recorder = CallHistoryRecorder(max_records=2)

    first = recorder.start_call(direction="outbound", dialed_number="5550001", started_at=_at(0))
    recorder.finalize(first, status="failed", ended_at=_at(1), termination_reason="busy")
    second = recorder.start_call(direction="outbound", dialed_number="5550002", started_at=_at(2))
    recorder.finalize(second, status="failed", ended_at=_at(3), termination_reason="no_answer")
    third = recorder.start_call(direction="outbound", dialed_number="5550003", started_at=_at(4))
    recorder.finalize(third, status="failed", ended_at=_at(5), termination_reason="no_carrier")

    recent = recorder.recent(limit=2)

    assert [record.dialed_number for record in recent] == ["5550003", "5550002"]
    assert all(record.dialed_number != "5550001" for record in recent)
    with pytest.raises(ValueError, match="limit"):
        recorder.recent(limit=0)
    with pytest.raises(ValueError, match="limit"):
        recorder.recent(limit=True)


def test_public_rendering_preserves_authenticated_shape_without_pii_by_default():
    recorder = CallHistoryRecorder(max_records=10)
    call_id = recorder.start_call(
        direction="outbound",
        dialed_number="+15551230101",
        started_at=_at(1),
    )
    recorder.mark_answered(call_id, answered_at=_at(5))
    record = recorder.finalize(
        call_id,
        status="ended",
        ended_at=_at(35),
        termination_reason="local_hangup",
        voicemail_id="vm-001",
    )

    rendered = render_call_record(record)

    assert rendered == {
        "id": record.id,
        "direction": "outbound",
        "status": "ended",
        "started_at": "2026-01-01T12:00:01+00:00",
        "answered_at": "2026-01-01T12:00:05+00:00",
        "ended_at": "2026-01-01T12:00:35+00:00",
        "duration_seconds": 30.0,
        "caller": "unknown",
        "dialed_number": "+***0101",
        "termination_reason": "local_hangup",
        "voicemail_id": "vm-001",
    }
    assert "+155****0101" not in str(rendered)


def test_finalized_records_are_not_treated_as_active_calls():
    recorder = CallHistoryRecorder(max_records=10)
    call_id = recorder.start_call(direction="inbound", caller="5550102", started_at=_at(0))

    recorder.finalize(call_id, status="missed", ended_at=_at(4), termination_reason="missed")

    with pytest.raises(KeyError, match="unknown active call record"):
        recorder.mark_answered(call_id, answered_at=_at(5))


def test_active_records_are_not_evicted_before_they_can_be_finalized():
    recorder = CallHistoryRecorder(max_records=1)
    first = recorder.start_call(direction="inbound", caller="5550103", started_at=_at(0))
    second = recorder.start_call(direction="outbound", dialed_number="5550104", started_at=_at(1))

    first_record = recorder.finalize(
        first, status="missed", ended_at=_at(2), termination_reason="missed"
    )
    second_record = recorder.finalize(
        second, status="failed", ended_at=_at(3), termination_reason="busy"
    )

    assert first_record.id == first
    assert recorder.recent(limit=1) == [second_record]


def test_duplicate_call_ids_are_rejected_before_history_is_corrupted():
    recorder = CallHistoryRecorder(max_records=10)
    call_id = recorder.start_call(direction="inbound", caller="5550105", started_at=_at(0))

    with pytest.raises(ValueError, match="call_id"):
        recorder.start_call(direction="outbound", dialed_number="5550106", call_id=call_id)


def test_finalize_rejects_non_terminal_status_without_closing_the_record():
    recorder = CallHistoryRecorder(max_records=10)
    call_id = recorder.start_call(direction="inbound", caller="5550107", started_at=_at(0))

    with pytest.raises(ValueError, match="terminal"):
        recorder.finalize(call_id, status="connected", ended_at=_at(1))

    record = recorder.finalize(call_id, status="missed", ended_at=_at(2))
    assert record.status == "missed"


def test_explicit_datetimes_must_be_timezone_aware():
    recorder = CallHistoryRecorder(max_records=10)
    naive = datetime(2026, 1, 1, 12, 0, 0)

    with pytest.raises(ValueError, match="timezone-aware"):
        recorder.start_call(direction="inbound", caller="5550108", started_at=naive)

    call_id = recorder.start_call(direction="inbound", caller="5550108", started_at=_at(0))
    with pytest.raises(ValueError, match="timezone-aware"):
        recorder.mark_answered(call_id, answered_at=naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        recorder.finalize(call_id, ended_at=naive)


def test_record_repr_does_not_include_raw_caller_or_dialed_number():
    recorder = CallHistoryRecorder(max_records=10)
    call_id = recorder.start_call(
        direction="outbound", dialed_number="5551234", started_at=_at(0)
    )

    record = recorder.finalize(call_id, status="failed", ended_at=_at(1))

    assert "5551234" not in repr(record)
