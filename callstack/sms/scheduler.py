"""In-memory scheduler kernel for sending SMS jobs at their due time."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, List, Optional, Protocol


class SendResult(Protocol):
    reference: Optional[int]


@dataclass
class ScheduledSMS:
    recipient: str
    body: str
    send_at: datetime
    status: str = "pending"
    sent_at: Optional[datetime] = None
    reference: Optional[int] = None
    last_error: Optional[str] = None


class SMSScheduler:
    def __init__(
        self,
        sender: Callable[[str, str], Awaitable[Optional[SendResult]]],
        jobs: List[ScheduledSMS],
    ):
        self._sender = sender
        self._jobs = jobs
        self._lock = asyncio.Lock()

    async def run_due_once(self, now: datetime) -> None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")

        async with self._lock:
            now_utc = now.astimezone(timezone.utc)

            for job in self._jobs:
                if job.send_at.tzinfo is None or job.send_at.utcoffset() is None:
                    raise ValueError("send_at must be timezone-aware")

            for job in self._jobs:
                if job.status != "pending" or job.send_at.astimezone(timezone.utc) > now_utc:
                    continue

                try:
                    result = await self._sender(job.recipient, job.body)
                except Exception as exc:
                    job.status = "failed"
                    job.last_error = type(exc).__name__
                    continue

                job.status = "sent"
                job.sent_at = now
                job.reference = getattr(result, "reference", None)
