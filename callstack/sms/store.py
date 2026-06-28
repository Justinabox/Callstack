"""SMS persistence: in-memory store with optional SQLite backend."""

import asyncio
import importlib
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from callstack.sms.types import DeliveryReport, SMS

logger = logging.getLogger("callstack.sms.store")

# SQL for creating the messages table
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender TEXT NOT NULL DEFAULT '',
    recipient TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    timestamp TEXT,
    status TEXT NOT NULL DEFAULT '',
    reference INTEGER NOT NULL DEFAULT 0,
    storage_index INTEGER
)
"""

_CREATE_DELIVERY_REPORTS_TABLE = """
CREATE TABLE IF NOT EXISTS delivery_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reference INTEGER NOT NULL DEFAULT 0,
    recipient TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    timestamp TEXT,
    discharge_time TEXT,
    message_id INTEGER
)
"""


class SMSStore:
    """In-memory SMS store with optional SQLite persistence.

    When db_path is provided, messages are also written to SQLite
    for survival across restarts. The in-memory list is the primary
    working set; SQLite is write-through.
    """

    def __init__(self, db_path: Optional[str] = None):
        self._messages: list[SMS] = []
        self._delivery_reports: list[DeliveryReport] = []
        self._next_id = 1
        self._next_report_id = 1
        self._db_path = db_path
        self._db = None
        self._lock = asyncio.Lock()
        # Saves made while a SQLite-backed store is not connected are kept in
        # memory and flushed on the next initialize(). The bool records whether
        # the ID was auto-assigned by this instance, which lets reopen logic
        # resolve external SQLite ID collisions without dropping new messages.
        self._pending_saves: dict[int, tuple[SMS, bool]] = {}
        self._pending_delivery_reports: dict[int, tuple[DeliveryReport, bool]] = {}

    async def initialize(self) -> None:
        """Open SQLite connection if db_path was provided, and load existing messages."""
        if self._db_path is None:
            return

        async with self._lock:
            if self._db is not None:
                return
            try:
                import aiosqlite
            except ImportError:
                logger.warning("aiosqlite not installed; SMS persistence disabled")
                self._db = None
                return

            pending_saves = list(self._pending_saves.values())
            pending_delivery_reports = list(self._pending_delivery_reports.values())
            self._db = await aiosqlite.connect(self._db_path)
            try:
                await self._db.execute(_CREATE_TABLE)
                await self._db.execute(_CREATE_DELIVERY_REPORTS_TABLE)
                await self._db.commit()

                # Load existing messages from SQLite. Rebuild the in-memory
                # working set so close()+initialize() on the same store object
                # does not append persisted rows a second time. Dirty saves made
                # while SQLite was closed are then flushed deterministically.

                loaded_messages: list[SMS] = []
                loaded_ids: set[int] = set()
                loaded_index_by_id: dict[int, int] = {}
                next_id = 1
                async with self._db.execute(
                    "SELECT id, sender, recipient, body, timestamp, status, reference, storage_index "
                    "FROM messages ORDER BY id"
                ) as cursor:
                    async for row in cursor:
                        ts = datetime.fromisoformat(row[4]) if row[4] else None
                        sms = SMS(
                            id=row[0],
                            sender=row[1],
                            recipient=row[2],
                            body=row[3],
                            timestamp=ts,
                            status=row[5],
                            reference=row[6],
                            storage_index=row[7],
                        )
                        loaded_messages.append(sms)
                        if row[0] is not None:
                            loaded_ids.add(row[0])
                            loaded_index_by_id[row[0]] = len(loaded_messages) - 1
                            if row[0] >= next_id:
                                next_id = row[0] + 1

                reserved_pending_ids = {
                    sms.id for sms, _ in pending_saves if sms.id is not None
                }
                for sms, auto_assigned_id in pending_saves:
                    if sms.id is None:
                        sms.id = next_id
                        next_id += 1

                    if sms.id in loaded_ids and auto_assigned_id:
                        reserved_pending_ids.discard(sms.id)
                        while next_id in loaded_ids or next_id in reserved_pending_ids:
                            next_id += 1
                        sms.id = next_id
                        reserved_pending_ids.add(sms.id)
                        next_id += 1

                    ts_iso = sms.timestamp.isoformat() if sms.timestamp else None
                    if sms.id in loaded_ids:
                        await self._db.execute(
                            "UPDATE messages SET sender=?, recipient=?, body=?, timestamp=?, "
                            "status=?, reference=?, storage_index=? WHERE id=?",
                            (
                                sms.sender, sms.recipient, sms.body, ts_iso,
                                sms.status, sms.reference, sms.storage_index, sms.id,
                            ),
                        )
                        loaded_messages[loaded_index_by_id[sms.id]] = sms
                    else:
                        await self._db.execute(
                            "INSERT INTO messages (id, sender, recipient, body, timestamp, status, reference, storage_index) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                sms.id, sms.sender, sms.recipient, sms.body, ts_iso,
                                sms.status, sms.reference, sms.storage_index,
                            ),
                        )
                        loaded_messages.append(sms)
                        loaded_ids.add(sms.id)
                        loaded_index_by_id[sms.id] = len(loaded_messages) - 1
                    if sms.id >= next_id:
                        next_id = sms.id + 1

                if pending_saves:
                    await self._db.commit()
                    self._pending_saves.clear()

                loaded_messages.sort(key=lambda sms: sms.id or 0)
                self._messages = loaded_messages
                self._next_id = next_id

                loaded_reports: list[DeliveryReport] = []
                next_report_id = 1
                async with self._db.execute(
                    "SELECT id, reference, recipient, status, timestamp, discharge_time, message_id "
                    "FROM delivery_reports ORDER BY id"
                ) as cursor:
                    async for row in cursor:
                        timestamp = datetime.fromisoformat(row[4]) if row[4] else None
                        discharge_time = datetime.fromisoformat(row[5]) if row[5] else None
                        report = DeliveryReport(
                            id=row[0],
                            reference=row[1],
                            recipient=row[2],
                            status=row[3],
                            timestamp=timestamp,
                            discharge_time=discharge_time,
                            message_id=row[6],
                        )
                        loaded_reports.append(report)
                        if row[0] is not None and row[0] >= next_report_id:
                            next_report_id = row[0] + 1

                self._delivery_reports = loaded_reports
                self._next_report_id = next_report_id

                loaded_report_ids = {report.id for report in loaded_reports if report.id is not None}
                for report, auto_assigned_id in pending_delivery_reports:
                    if report.id in loaded_report_ids and auto_assigned_id:
                        while self._next_report_id in loaded_report_ids:
                            self._next_report_id += 1
                        report.id = self._next_report_id
                        self._next_report_id += 1
                    await self._save_delivery_report_locked(report, commit=False)
                    if report.id is not None:
                        loaded_report_ids.add(report.id)
                if pending_delivery_reports:
                    await self._db.commit()
                    self._pending_delivery_reports.clear()

                logger.info(
                    "SMS store initialized with SQLite: %s (%d messages loaded)",
                    self._db_path, len(self._messages),
                )
            except Exception:
                await self._db.close()
                self._db = None
                raise

    async def close(self) -> None:
        """Close the SQLite connection if open."""
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def save(self, sms: SMS) -> SMS:
        """Save an SMS message. Assigns an ID if not set. Updates if ID already exists."""
        async with self._lock:
            auto_assigned_id = sms.id is None
            if sms.id is None:
                sms.id = self._next_id
                self._next_id += 1

            # Check if message with this ID already exists (update vs insert)
            existing_index = None
            for i, msg in enumerate(self._messages):
                if msg.id == sms.id:
                    existing_index = i
                    break

            # Write to SQLite first so a DB failure doesn't desync in-memory state
            if self._db is not None:
                ts_iso = sms.timestamp.isoformat() if sms.timestamp else None
                if existing_index is not None:
                    await self._db.execute(
                        "UPDATE messages SET sender=?, recipient=?, body=?, timestamp=?, "
                        "status=?, reference=?, storage_index=? WHERE id=?",
                        (
                            sms.sender, sms.recipient, sms.body, ts_iso,
                            sms.status, sms.reference, sms.storage_index, sms.id,
                        ),
                    )
                else:
                    await self._db.execute(
                        "INSERT INTO messages (id, sender, recipient, body, timestamp, status, reference, storage_index) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            sms.id, sms.sender, sms.recipient, sms.body, ts_iso,
                            sms.status, sms.reference, sms.storage_index,
                        ),
                    )
                await self._db.commit()
            elif self._db_path is not None:
                previous = self._pending_saves.get(sms.id)
                pending_auto_assigned = auto_assigned_id or (previous[1] if previous else False)
                self._pending_saves[sms.id] = (sms, pending_auto_assigned)

            # Now update in-memory state
            if existing_index is not None:
                self._messages[existing_index] = sms
            else:
                self._messages.append(sms)

            return sms

    async def save_delivery_report(self, report: DeliveryReport) -> DeliveryReport:
        """Save a delivery report and update the latest matching outbound SMS."""
        async with self._lock:
            return await self._save_delivery_report_locked(report)

    async def _save_delivery_report_locked(
        self, report: DeliveryReport, *, commit: bool = True
    ) -> DeliveryReport:
        auto_assigned_id = report.id is None
        if report.id is None:
            report.id = self._next_report_id
            self._next_report_id += 1
        if report.timestamp is None:
            report.timestamp = datetime.now(timezone.utc)

        matched = self._latest_matching_message(report)
        if matched is not None:
            report.message_id = matched.id

        existing_index = None
        for index, existing in enumerate(self._delivery_reports):
            if existing.id == report.id:
                existing_index = index
                break

        if self._db is not None:
            report_ts = report.timestamp.isoformat() if report.timestamp else None
            discharge_ts = report.discharge_time.isoformat() if report.discharge_time else None
            if existing_index is None:
                await self._db.execute(
                    "INSERT INTO delivery_reports (id, reference, recipient, status, timestamp, discharge_time, message_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        report.id,
                        report.reference,
                        report.recipient,
                        report.status,
                        report_ts,
                        discharge_ts,
                        report.message_id,
                    ),
                )
            else:
                await self._db.execute(
                    "UPDATE delivery_reports SET reference=?, recipient=?, status=?, timestamp=?, "
                    "discharge_time=?, message_id=? WHERE id=?",
                    (
                        report.reference,
                        report.recipient,
                        report.status,
                        report_ts,
                        discharge_ts,
                        report.message_id,
                        report.id,
                    ),
                )
            if matched is not None:
                message_ts = matched.timestamp.isoformat() if matched.timestamp else None
                await self._db.execute(
                    "UPDATE messages SET sender=?, recipient=?, body=?, timestamp=?, "
                    "status=?, reference=?, storage_index=? WHERE id=?",
                    (
                        matched.sender,
                        matched.recipient,
                        matched.body,
                        message_ts,
                        report.status,
                        matched.reference,
                        matched.storage_index,
                        matched.id,
                    ),
                )
            if commit:
                await self._db.commit()
        elif self._db_path is not None:
            previous = self._pending_delivery_reports.get(report.id)
            pending_auto_assigned = auto_assigned_id or (previous[1] if previous else False)
            self._pending_delivery_reports[report.id] = (report, pending_auto_assigned)

        if matched is not None:
            matched.status = report.status
        if existing_index is None:
            self._delivery_reports.append(report)
        else:
            self._delivery_reports[existing_index] = report
        return report

    def _latest_matching_message(self, report: DeliveryReport) -> SMS | None:
        if not report.recipient:
            return None
        for sms in reversed(self._messages):
            if sms.id is None or not sms.recipient:
                continue
            if sms.reference == report.reference and sms.recipient == report.recipient:
                return sms
        return None

    async def list_delivery_reports(self, limit: int = 100) -> list[DeliveryReport]:
        """List saved delivery reports, newest last, with a bounded result size."""
        async with self._lock:
            return self._delivery_reports[-limit:]

    async def get(self, id: int) -> Optional[SMS]:
        """Get an SMS by internal ID."""
        async with self._lock:
            for msg in self._messages:
                if msg.id == id:
                    return msg
            return None

    async def list(
        self,
        sender: Optional[str] = None,
        recipient: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[SMS]:
        """List messages with optional filters."""
        async with self._lock:
            results = self._messages
            if sender:
                results = [m for m in results if m.sender == sender]
            if recipient:
                results = [m for m in results if m.recipient == recipient]
            if status:
                results = [m for m in results if m.status == status]
            return results[-limit:]

    async def delete(self, id: int) -> bool:
        """Delete a message by internal ID."""
        async with self._lock:
            target_index = None
            for i, msg in enumerate(self._messages):
                if msg.id == id:
                    target_index = i
                    break

            if target_index is None:
                return False

            pending = self._pending_saves.get(id)
            pending_auto_assigned = pending[1] if pending is not None else False
            should_delete_from_closed_db = (
                self._db is None
                and self._db_path is not None
                and os.path.exists(self._db_path)
                and not pending_auto_assigned
            )

            if self._db is not None:
                await self._db.execute("DELETE FROM messages WHERE id = ?", (id,))
                await self._db.commit()
            elif should_delete_from_closed_db:
                try:
                    aiosqlite = importlib.import_module("aiosqlite")
                except ImportError:
                    logger.warning("aiosqlite not installed; SMS persistence disabled")
                    return False

                db = await aiosqlite.connect(self._db_path)
                try:
                    await db.execute(_CREATE_TABLE)
                    await db.execute("DELETE FROM messages WHERE id = ?", (id,))
                    await db.commit()
                finally:
                    await db.close()

            self._messages.pop(target_index)
            self._pending_saves.pop(id, None)
            return True

    async def count(self) -> int:
        """Return total message count."""
        async with self._lock:
            return len(self._messages)

    async def clear(self) -> None:
        """Delete all messages."""
        async with self._lock:
            self._messages.clear()
            self._pending_saves.clear()
            if self._db is not None:
                await self._db.execute("DELETE FROM messages")
                await self._db.commit()
            elif self._db_path is not None:
                try:
                    aiosqlite = importlib.import_module("aiosqlite")
                except ImportError:
                    logger.warning("aiosqlite not installed; SMS persistence disabled")
                    return

                db = await aiosqlite.connect(self._db_path)
                try:
                    await db.execute(_CREATE_TABLE)
                    await db.execute("DELETE FROM messages")
                    await db.commit()
                finally:
                    await db.close()
