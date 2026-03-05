"""SMS persistence: in-memory store with optional SQLite backend."""

import asyncio
import logging
from typing import Optional

from callstack.sms.types import SMS

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


class SMSStore:
    """In-memory SMS store with optional SQLite persistence.

    When db_path is provided, messages are also written to SQLite
    for survival across restarts. The in-memory list is the primary
    working set; SQLite is write-through.
    """

    def __init__(self, db_path: Optional[str] = None):
        self._messages: list[SMS] = []
        self._next_id = 1
        self._db_path = db_path
        self._db = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Open SQLite connection if db_path was provided, and load existing messages."""
        if self._db_path is None:
            return
        try:
            import aiosqlite
        except ImportError:
            logger.warning("aiosqlite not installed; SMS persistence disabled")
            self._db = None
            return

        self._db = await aiosqlite.connect(self._db_path)
        try:
            await self._db.execute(_CREATE_TABLE)
            await self._db.commit()

            # Load existing messages from SQLite
            from datetime import datetime
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
                    self._messages.append(sms)
                    if row[0] is not None and row[0] >= self._next_id:
                        self._next_id = row[0] + 1

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

            # Now update in-memory state
            if existing_index is not None:
                self._messages[existing_index] = sms
            else:
                self._messages.append(sms)

            return sms

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
            for i, msg in enumerate(self._messages):
                if msg.id == id:
                    self._messages.pop(i)
                    if self._db is not None:
                        await self._db.execute("DELETE FROM messages WHERE id = ?", (id,))
                        await self._db.commit()
                    return True
            return False

    async def count(self) -> int:
        """Return total message count."""
        async with self._lock:
            return len(self._messages)

    async def clear(self) -> None:
        """Delete all messages."""
        async with self._lock:
            self._messages.clear()
            if self._db is not None:
                await self._db.execute("DELETE FROM messages")
                await self._db.commit()
