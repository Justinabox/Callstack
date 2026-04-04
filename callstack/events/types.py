from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto


class CallState(Enum):
    IDLE = auto()
    DIALING = auto()
    RINGING = auto()
    ACTIVE = auto()
    HELD = auto()
    ENDED = auto()


@dataclass(frozen=True)
class Event:
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass(frozen=True)
class RingEvent(Event):
    pass


@dataclass(frozen=True)
class CallerIDEvent(Event):
    number: str = ""


@dataclass(frozen=True)
class DTMFEvent(Event):
    digit: str = ""


@dataclass(frozen=True)
class CallStateEvent(Event):
    state: CallState = CallState.IDLE


@dataclass(frozen=True)
class IncomingSMSEvent(Event):
    sender: str = ""
    body: str = ""
    raw: str = ""


@dataclass(frozen=True)
class SMSSentEvent(Event):
    recipient: str = ""
    reference: int = 0


@dataclass(frozen=True)
class SignalQualityEvent(Event):
    rssi: int = 0
    ber: int = 0


@dataclass(frozen=True)
class ModemDisconnectedEvent(Event):
    reason: str = ""


@dataclass(frozen=True)
class ModemReconnectedEvent(Event):
    pass


@dataclass(frozen=True)
class SMSDeliveryReportEvent(Event):
    """Delivery status report for a sent SMS."""
    reference: int = 0
    recipient: str = ""
    status: str = ""  # "delivered", "failed", "pending"


@dataclass(frozen=True)
class USSDResponseEvent(Event):
    """Response from a USSD command."""
    status: int = 0  # 0=no further action, 1=further action, 2=terminated
    message: str = ""
    encoding: int = 15


@dataclass(frozen=True)
class _RawSMSNotification(Event):
    """Internal event for raw SMS URC data. Not part of the public API."""
    sender: str = ""
    body: str = ""
    raw: str = ""


@dataclass(frozen=True)
class _RawDeliveryReport(Event):
    """Internal event for raw delivery report URC. Not part of the public API."""
    storage: str = ""
    index: int = 0
