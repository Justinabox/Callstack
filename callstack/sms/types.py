"""SMS data types."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class SMSStatus(Enum):
    UNREAD = "REC UNREAD"
    READ = "REC READ"
    UNSENT = "STO UNSENT"
    SENT = "STO SENT"
    ALL = "ALL"


@dataclass
class SMS:
    sender: str = ""
    recipient: str = ""
    body: str = ""
    timestamp: Optional[datetime] = None
    status: str = ""
    reference: int = 0
    id: Optional[int] = None
    storage_index: Optional[int] = None

    @property
    def is_incoming(self) -> bool:
        return self.status in (SMSStatus.UNREAD.value, SMSStatus.READ.value, "unread", "read")


@dataclass
class DeliveryReport:
    reference: int = 0
    recipient: str = ""
    status: str = ""
    timestamp: Optional[datetime] = None
    discharge_time: Optional[datetime] = None
