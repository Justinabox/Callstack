"""Pure hardware discovery and profile helpers."""

from callstack.hardware.discovery import (
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
from callstack.hardware.profiles import classify_capabilities, profile_notes

__all__ = [
    "CapabilityStatus",
    "ModemCapabilities",
    "ModemDiscoveryReport",
    "ModemIdentity",
    "DEFAULT_DISCOVERY_PATTERNS",
    "SAFE_PROBE_COMMANDS",
    "classify_capabilities",
    "discover_modems",
    "probe_modem_ports",
    "profile_notes",
]
