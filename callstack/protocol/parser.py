"""AT response parsing utilities."""

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ParsedResponse:
    """Structured data extracted from an AT response."""
    command: str = ""
    values: dict[str, str] = field(default_factory=dict)
    raw: str = ""


class ATResponseParser:
    """Parses structured data from AT command response lines."""

    # Pattern for +CMD: value responses
    _PREFIX_RE = re.compile(r"^\+([A-Z]+):\s*(.*)$")

    # Signal quality: +CSQ: rssi,ber
    _CSQ_RE = re.compile(r"^\+CSQ:\s*(\d+),(\d+)$")

    # Registration: +CREG: n,stat
    _CREG_RE = re.compile(r"^\+CREG:\s*(\d+),(\d+)")

    # Caller ID: +CLIP: "number",type
    _CLIP_RE = re.compile(r'^\+CLIP:\s*"([^"]*)"')

    # SMS send ref: +CMGS: ref
    _CMGS_RE = re.compile(r"^\+CMGS:\s*(\d+)")

    # SMS notification: +CMTI: "storage",index
    _CMTI_RE = re.compile(r'^\+CMTI:\s*"([^"]*)",(\d+)')

    # SMS header: +CMT: "sender","","timestamp"
    _CMT_RE = re.compile(r'^\+CMT:\s*"([^"]*)"')

    @staticmethod
    def parse_signal_quality(line: str) -> Optional[tuple[int, int]]:
        """Parse +CSQ response. Returns (rssi, ber) or None."""
        m = ATResponseParser._CSQ_RE.match(line)
        if m:
            return int(m.group(1)), int(m.group(2))
        return None

    @staticmethod
    def parse_registration(line: str) -> Optional[tuple[int, int]]:
        """Parse +CREG response. Returns (mode, status) or None."""
        m = ATResponseParser._CREG_RE.match(line)
        if m:
            return int(m.group(1)), int(m.group(2))
        return None

    @staticmethod
    def parse_clip(line: str) -> Optional[str]:
        """Parse +CLIP caller ID. Returns phone number or None."""
        m = ATResponseParser._CLIP_RE.match(line)
        if m:
            return m.group(1)
        return None

    @staticmethod
    def parse_cmgs(line: str) -> Optional[int]:
        """Parse +CMGS SMS send reference number."""
        m = ATResponseParser._CMGS_RE.match(line)
        if m:
            return int(m.group(1))
        return None

    @staticmethod
    def parse_cmti(line: str) -> Optional[tuple[str, int]]:
        """Parse +CMTI notification. Returns (storage, index) or None."""
        m = ATResponseParser._CMTI_RE.match(line)
        if m:
            return m.group(1), int(m.group(2))
        return None

    @staticmethod
    def parse_cmt(line: str) -> Optional[str]:
        """Parse +CMT sender. Returns phone number or None."""
        m = ATResponseParser._CMT_RE.match(line)
        if m:
            return m.group(1)
        return None

    @staticmethod
    def parse_prefix(line: str) -> Optional[ParsedResponse]:
        """Parse any +CMD: value line into a ParsedResponse."""
        m = ATResponseParser._PREFIX_RE.match(line)
        if m:
            return ParsedResponse(command=m.group(1), raw=m.group(2))
        return None
