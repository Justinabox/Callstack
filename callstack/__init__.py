"""Callstack - Async-first GSM/LTE modem telephony framework.

Top-level symbols are loaded lazily so pure subpackages can be imported without
pulling optional serial transport dependencies into deterministic unit tests.
"""

from importlib import import_module
from typing import Any

_EXPORTS: dict[str, tuple[str, str]] = {
    # Top-level
    "Modem": ("callstack.modem", "Modem"),
    "ModemConfig": ("callstack.config", "ModemConfig"),
    # Errors
    "CallstackError": ("callstack.errors", "CallstackError"),
    "ATError": ("callstack.errors", "ATError"),
    "ATTimeoutError": ("callstack.errors", "ATTimeoutError"),
    "ATCommandError": ("callstack.errors", "ATCommandError"),
    "InvalidStateTransition": ("callstack.errors", "InvalidStateTransition"),
    "TransportError": ("callstack.errors", "TransportError"),
    "SIMError": ("callstack.errors", "SIMError"),
    "SIMPINRequired": ("callstack.errors", "SIMPINRequired"),
    "SIMPUKRequired": ("callstack.errors", "SIMPUKRequired"),
    "SIMUnlockError": ("callstack.errors", "SIMUnlockError"),
    "SMSError": ("callstack.errors", "SMSError"),
    "SMSSendError": ("callstack.errors", "SMSSendError"),
    "SMSReadError": ("callstack.errors", "SMSReadError"),
    # Events
    "Event": ("callstack.events.types", "Event"),
    "CallState": ("callstack.events.types", "CallState"),
    "RingEvent": ("callstack.events.types", "RingEvent"),
    "CallerIDEvent": ("callstack.events.types", "CallerIDEvent"),
    "DTMFEvent": ("callstack.events.types", "DTMFEvent"),
    "CallStateEvent": ("callstack.events.types", "CallStateEvent"),
    "IncomingSMSEvent": ("callstack.events.types", "IncomingSMSEvent"),
    "SMSSentEvent": ("callstack.events.types", "SMSSentEvent"),
    "SMSDeliveryReportEvent": ("callstack.events.types", "SMSDeliveryReportEvent"),
    "USSDResponseEvent": ("callstack.events.types", "USSDResponseEvent"),
    "SignalQualityEvent": ("callstack.events.types", "SignalQualityEvent"),
    "ModemDisconnectedEvent": ("callstack.events.types", "ModemDisconnectedEvent"),
    "ModemReconnectedEvent": ("callstack.events.types", "ModemReconnectedEvent"),
    "EventBus": ("callstack.events.bus", "EventBus"),
    # Transport
    "Transport": ("callstack.transport.base", "Transport"),
    # Protocol
    "ATCommandExecutor": ("callstack.protocol.executor", "ATCommandExecutor"),
    "ATResponse": ("callstack.protocol.executor", "ATResponse"),
    # Voice
    "CallStateMachine": ("callstack.voice.state", "CallStateMachine"),
    "AudioPlayer": ("callstack.voice.player", "AudioPlayer"),
    "AudioPipeline": ("callstack.voice.audio", "AudioPipeline"),
    "CallService": ("callstack.voice.service", "CallService"),
    "CallSession": ("callstack.voice.service", "CallSession"),
    "DTMFCollector": ("callstack.voice.dtmf", "DTMFCollector"),
    "IVRMenu": ("callstack.voice.ivr", "IVRMenu"),
    "IVRFlow": ("callstack.voice.ivr", "IVRFlow"),
    "MenuOption": ("callstack.voice.ivr", "MenuOption"),
    # SMS
    "SMS": ("callstack.sms.types", "SMS"),
    "DeliveryReport": ("callstack.sms.types", "DeliveryReport"),
    "SMSStatus": ("callstack.sms.types", "SMSStatus"),
    "SMSStore": ("callstack.sms.store", "SMSStore"),
    "SMSService": ("callstack.sms.service", "SMSService"),
    # Network
    "NetworkService": ("callstack.network", "NetworkService"),
    "SignalInfo": ("callstack.network", "SignalInfo"),
    "RegistrationInfo": ("callstack.network", "RegistrationInfo"),
    # USSD
    "USSDService": ("callstack.ussd", "USSDService"),
    # Utilities
    "retry": ("callstack.utils.retry", "retry"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    """Load exported symbols on first access."""

    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attribute = _EXPORTS[name]
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted((*globals(), *__all__))
