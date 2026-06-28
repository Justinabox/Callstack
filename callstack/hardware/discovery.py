"""Pure modem discovery result types.

This module intentionally contains only deterministic data containers. It does
not scan ports, open serial devices, or send AT commands.
"""

from dataclasses import dataclass, field, fields
from typing import Literal

CapabilityStatus = Literal["supported", "unsupported", "unknown"]
AudioPortConfidence = Literal["configured", "profile-hint", "sibling-serial", "unknown"]
_VALID_CAPABILITY_STATUSES = {"supported", "unsupported", "unknown"}
_VALID_AUDIO_PORT_CONFIDENCES = {"configured", "profile-hint", "sibling-serial", "unknown"}
UNKNOWN_AUDIO_PORT_REASON = "Audio port role is unknown; configure CALLSTACK_AUDIO_PORT after hardware validation."


@dataclass(frozen=True)
class AudioPortHint:
    """Public-safe audio-port assignment hint.

    Discovery never proves an audio role by opening live audio/call paths. This
    type keeps configured values and conservative hints visibly separate from a
    verified modem setting.
    """

    port: str | None = None
    confidence: AudioPortConfidence = "unknown"
    reason: str = UNKNOWN_AUDIO_PORT_REASON

    def __post_init__(self) -> None:
        if self.confidence not in _VALID_AUDIO_PORT_CONFIDENCES:
            raise ValueError(
                f"audio port confidence must be one of {sorted(_VALID_AUDIO_PORT_CONFIDENCES)}, "
                f"got {self.confidence!r}"
            )


def unknown_audio_port_hint() -> AudioPortHint:
    """Return the default public-safe unknown audio-port hint."""

    return AudioPortHint()


@dataclass(frozen=True)
class ModemIdentity:
    """Non-sensitive modem identity facts gathered elsewhere.

    Sensitive identifiers are deliberately not represented. Use
    ``imei_present`` only to record that an IMEI-like response existed without
    storing the identifier value.
    """

    manufacturer: str = ""
    model: str = ""
    revision: str = ""
    imei_present: bool = False


@dataclass(frozen=True)
class ModemCapabilities:
    """Conservative modem capability profile.

    Capabilities default to ``unknown`` until deterministic profile evidence or
    a future active probe establishes support.
    """

    sms_text_mode: CapabilityStatus = "unknown"
    sms_pdu_mode: CapabilityStatus = "unknown"
    delivery_reports: CapabilityStatus = "unknown"
    ussd: CapabilityStatus = "unknown"
    voice_calls: CapabilityStatus = "unknown"
    dtmf_send: CapabilityStatus = "unknown"
    pcm_audio: CapabilityStatus = "unknown"
    gnss: CapabilityStatus = "unknown"

    def __post_init__(self) -> None:
        for capability in fields(self):
            value = getattr(self, capability.name)
            if value not in _VALID_CAPABILITY_STATUSES:
                raise ValueError(
                    f"{capability.name} must be one of "
                    f"{sorted(_VALID_CAPABILITY_STATUSES)}, got {value!r}"
                )


@dataclass(frozen=True)
class ModemDiscoveryReport:
    """Pure summary of a modem discovery/classification result."""

    at_port: str
    audio_port: str | None = None
    identity: ModemIdentity = field(default_factory=ModemIdentity)
    capabilities: ModemCapabilities = field(default_factory=ModemCapabilities)
    confidence: str = "unknown"
    notes: tuple[str, ...] = ()
    audio_hint: AudioPortHint = field(default_factory=unknown_audio_port_hint)
