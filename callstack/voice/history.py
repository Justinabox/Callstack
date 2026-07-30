"""Bounded, privacy-aware call history helpers."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from callstack.privacy import redact_phone_number

CallDirection = Literal["inbound", "outbound", "unknown"]
CallStatus = Literal["ringing", "connected", "missed", "ended", "failed"]
_TERMINAL_STATUSES: set[CallStatus] = {"missed", "ended", "failed"}


@dataclass(frozen=True)
class CallRecord:
    """One bounded call-history entry for a single call attempt/session."""

    id: str
    direction: CallDirection
    status: CallStatus
    started_at: datetime
    answered_at: datetime | None = None
    ended_at: datetime | None = None
    duration_seconds: float | None = None
    caller: str = field(default="", repr=False)
    dialed_number: str = field(default="", repr=False)
    termination_reason: str = "unknown"
    voicemail_id: str | None = None


class CallHistoryRecorder:
    """Process-local bounded call-history recorder.

    The recorder deliberately stores only structured call metadata. Rendering is
    PII-safe by default so logs, dashboards, and public feeds can reuse the same
    helper without exposing raw caller or dialed identifiers.
    """

    def __init__(self, max_records: int = 200):
        if type(max_records) is not int or max_records < 1:
            raise ValueError("max_records must be a positive integer")
        self._max_records = max_records
        self._records: list[CallRecord] = []
        self._active: dict[str, CallRecord] = {}

    def start_call(
        self,
        *,
        direction: CallDirection = "unknown",
        caller: str = "",
        dialed_number: str = "",
        started_at: datetime | None = None,
        call_id: str | None = None,
    ) -> str:
        """Start tracking a call and return its stable record id."""
        timestamp = _require_aware_datetime(started_at, "started_at") or datetime.now(
            timezone.utc
        )
        record_id = call_id or uuid4().hex
        if any(existing.id == record_id for existing in self._records):
            raise ValueError("call_id must be unique")

        record = CallRecord(
            id=record_id,
            direction=direction,
            status="ringing",
            started_at=timestamp,
            caller=caller,
            dialed_number=dialed_number,
        )
        self._records.append(record)
        self._active[record.id] = record
        self._enforce_bound()
        return record.id

    def mark_answered(
        self, call_id: str, *, answered_at: datetime | None = None
    ) -> CallRecord:
        """Mark an active call as connected."""
        record = self._require_active(call_id)
        timestamp = _require_aware_datetime(answered_at, "answered_at") or datetime.now(
            timezone.utc
        )
        updated = replace(
            record,
            status="connected",
            answered_at=timestamp,
        )
        return self._replace_record(updated, active=True)

    def finalize(
        self,
        call_id: str,
        *,
        status: CallStatus = "ended",
        ended_at: datetime | None = None,
        termination_reason: str = "unknown",
        voicemail_id: str | None = None,
    ) -> CallRecord:
        """Finalize an active call record and return the immutable snapshot."""
        if status not in _TERMINAL_STATUSES:
            raise ValueError("finalize status must be terminal")
        record = self._require_active(call_id)
        finished_at = _require_aware_datetime(ended_at, "ended_at") or datetime.now(
            timezone.utc
        )
        duration_start = record.answered_at or record.started_at
        duration = max(0.0, (finished_at - duration_start).total_seconds())
        updated = replace(
            record,
            status=status,
            ended_at=finished_at,
            duration_seconds=duration,
            termination_reason=termination_reason,
            voicemail_id=voicemail_id,
        )
        self._active.pop(call_id, None)
        updated = self._replace_record(updated, active=False)
        self._enforce_bound()
        return updated

    def recent(self, *, limit: int | None = None) -> list[CallRecord]:
        """Return recent records newest-first."""
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("limit must be a positive integer")
        records = list(reversed(self._records))
        return records if limit is None else records[:limit]

    def _require_active(self, call_id: str) -> CallRecord:
        try:
            return self._active[call_id]
        except KeyError as exc:
            raise KeyError("unknown active call record") from exc

    def _replace_record(self, record: CallRecord, *, active: bool) -> CallRecord:
        for index, existing in enumerate(self._records):
            if existing.id == record.id:
                self._records[index] = record
                break
        if active:
            self._active[record.id] = record
        else:
            self._active.pop(record.id, None)
        return record

    def _enforce_bound(self) -> None:
        while len(self._records) > self._max_records:
            remove_index = next(
                (
                    index
                    for index, record in enumerate(self._records)
                    if record.id not in self._active
                ),
                None,
            )
            if remove_index is None:
                break
            removed = self._records.pop(remove_index)
            self._active.pop(removed.id, None)


def _format_timestamp(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _require_aware_datetime(value: datetime | None, name: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def render_call_record(record: CallRecord, *, redact: bool = True) -> dict[str, object]:
    """Render a call record as JSON-ready public-safe metadata."""
    caller = redact_phone_number(record.caller) if redact else record.caller or "unknown"
    dialed_number = (
        redact_phone_number(record.dialed_number)
        if redact
        else record.dialed_number or "unknown"
    )
    return {
        "id": record.id,
        "direction": record.direction,
        "status": record.status,
        "started_at": _format_timestamp(record.started_at),
        "answered_at": _format_timestamp(record.answered_at),
        "ended_at": _format_timestamp(record.ended_at),
        "duration_seconds": record.duration_seconds,
        "caller": caller,
        "dialed_number": dialed_number,
        "termination_reason": record.termination_reason,
        "voicemail_id": record.voicemail_id,
    }
