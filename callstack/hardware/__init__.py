"""Pure hardware discovery and profile helpers."""

from callstack.hardware.discovery import (
    CapabilityStatus,
    ModemCapabilities,
    ModemDiscoveryReport,
    ModemIdentity,
)
from callstack.hardware.profiles import classify_capabilities, profile_notes

__all__ = [
    "CapabilityStatus",
    "ModemCapabilities",
    "ModemDiscoveryReport",
    "ModemIdentity",
    "classify_capabilities",
    "profile_notes",
]
