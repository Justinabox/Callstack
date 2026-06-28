"""Pure hardware discovery and profile helpers."""

from callstack.hardware.discovery import (
    AudioPortConfidence,
    AudioPortHint,
    CapabilityStatus,
    ModemCapabilities,
    ModemDiscoveryReport,
    ModemIdentity,
)
from callstack.hardware.probe import (
    DEFAULT_DISCOVERY_PATTERNS,
    SAFE_PROBE_COMMANDS,
    discover_modems,
    probe_modem_ports,
)
from callstack.hardware.profiles import audio_port_hint_for_identity, classify_capabilities, profile_notes

__all__ = [
    "AudioPortConfidence",
    "AudioPortHint",
    "CapabilityStatus",
    "ModemCapabilities",
    "ModemDiscoveryReport",
    "ModemIdentity",
    "DEFAULT_DISCOVERY_PATTERNS",
    "SAFE_PROBE_COMMANDS",
    "audio_port_hint_for_identity",
    "classify_capabilities",
    "discover_modems",
    "probe_modem_ports",
    "profile_notes",
]
