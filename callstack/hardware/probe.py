"""Safe, injectable modem bring-up probe helpers.

The probe in this module is deliberately narrow: it only sends non-mutating AT
attention/identity commands and stores only non-sensitive identity strings. It
never asks for IMEI, IMSI, ICCID, SIM phone number, storage contents, SIM unlock,
SMS, USSD, or call state.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import fields, replace
from typing import Protocol

from callstack.hardware.discovery import ModemCapabilities, ModemDiscoveryReport, ModemIdentity
from callstack.hardware.profiles import classify_capabilities, profile_notes
from callstack.transport.base import Transport
from callstack.transport.serial import SerialTransport

SAFE_PROBE_COMMANDS = ("AT", "ATI", "AT+GMI", "AT+GMM", "AT+GMR")
_TERMINAL_RESPONSES = {"OK", "ERROR"}
_IMEI_LINE_RE = re.compile(r"^(?:imei\s*[:=]?\s*)?\d{14,22}$", re.IGNORECASE)
_SENSITIVE_IDENTIFIER_TERMS = (
    "+gsn",
    "gsn",
    "imei",
    "imsi",
    "iccid",
    "cnum",
    "msisdn",
    "meid",
    "esn",
    "serial",
    "s/n",
    "sn:",
)


class TransportOpener(Protocol):
    """Factory/opener used by probe tests and production serial transport."""

    def __call__(self, port: str) -> Transport: ...


def _default_transport_opener(baudrate: int) -> TransportOpener:
    def _open(port: str) -> Transport:
        return SerialTransport(port, baudrate=baudrate)

    return _open


def _clean_line(line: bytes) -> str:
    return line.decode("utf-8", errors="replace").strip()


async def _send_safe_command(transport: Transport, command: str, timeout: float) -> tuple[list[str], str]:
    if command not in SAFE_PROBE_COMMANDS:
        raise ValueError(f"unsafe probe command rejected: {command}")

    await asyncio.wait_for(transport.write(f"{command}\r\n".encode("ascii")), timeout=timeout)
    deadline = asyncio.get_running_loop().time() + timeout
    lines: list[str] = []
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return lines, "timeout"
        try:
            raw_line = await asyncio.wait_for(transport.readline(), timeout=remaining)
        except asyncio.TimeoutError:
            return lines, "timeout"

        line = _clean_line(raw_line)
        if not line:
            continue
        if line == command:
            continue
        lines.append(line)
        if line.upper() in _TERMINAL_RESPONSES:
            return lines, line.upper()


def _response_values(lines: Sequence[str]) -> list[str]:
    return [line for line in lines if line.upper() not in _TERMINAL_RESPONSES]


def _is_sensitive_identifier_line(value: str) -> bool:
    normalized = value.strip()
    lowered = normalized.lower()
    if _IMEI_LINE_RE.fullmatch(normalized):
        return True
    return any(term in lowered for term in _SENSITIVE_IDENTIFIER_TERMS)


def _safe_response_values(lines: Sequence[str]) -> tuple[list[str], bool]:
    values = _response_values(lines)
    safe_values = [value for value in values if not _is_sensitive_identifier_line(value)]
    return safe_values, len(safe_values) != len(values)


def _strip_revision_prefix(value: str) -> str:
    if ":" in value and value.split(":", 1)[0].strip().lower() in {"revision", "rev"}:
        return value.split(":", 1)[1].strip()
    return value.strip()


def _identity_from_responses(responses: dict[str, list[str]]) -> ModemIdentity:
    manufacturer = ""
    model = ""
    revision = ""
    imei_present = False

    gmi_values, gmi_had_identifier = _safe_response_values(responses.get("AT+GMI", ()))
    gmm_values, gmm_had_identifier = _safe_response_values(responses.get("AT+GMM", ()))
    gmr_values, gmr_had_identifier = _safe_response_values(responses.get("AT+GMR", ()))
    ati_values, ati_had_identifier = _safe_response_values(responses.get("ATI", ()))
    imei_present = gmi_had_identifier or gmm_had_identifier or gmr_had_identifier or ati_had_identifier

    if gmi_values:
        manufacturer = gmi_values[0].strip()
    if gmm_values:
        model = gmm_values[0].strip()
    if gmr_values:
        revision = _strip_revision_prefix(gmr_values[0])

    if ati_values:
        if not manufacturer:
            manufacturer = ati_values[0].strip()
        if not model and len(ati_values) >= 2:
            model = ati_values[1].strip()
        if not revision:
            revision_candidates = [line for line in ati_values if "revision" in line.lower() or "rev" in line.lower()]
            if revision_candidates:
                revision = _strip_revision_prefix(revision_candidates[0])
            elif len(ati_values) >= 3:
                revision = _strip_revision_prefix(ati_values[2])

    return ModemIdentity(manufacturer=manufacturer, model=model, revision=revision, imei_present=imei_present)


def _has_supported_capability(capabilities: ModemCapabilities) -> bool:
    return any(getattr(capabilities, field.name) == "supported" for field in fields(capabilities))


def _confidence(identity: ModemIdentity, capabilities: ModemCapabilities, attention_ok: bool) -> str:
    if _has_supported_capability(capabilities):
        return "profile-match"
    if identity.manufacturer or identity.model or identity.revision:
        return "identity-response"
    if attention_ok:
        return "attention-response"
    return "no-response"


async def probe_modem_ports(
    candidate_ports: Sequence[str],
    *,
    transport_opener: TransportOpener | None = None,
    baudrate: int = 115200,
    command_timeout: float = 0.5,
) -> ModemDiscoveryReport:
    """Probe explicit candidate AT ports with safe identity commands.

    Args:
        candidate_ports: Explicit ports to try. The function never scans the
            host for additional devices.
        transport_opener: Injectable factory returning an unopened async
            transport for a candidate port.
        baudrate: Baudrate used by the default serial transport opener.
        command_timeout: Short per-command readline timeout in seconds.

    Returns:
        A PII-safe discovery report. Failed and timed-out candidate ports are
        represented as actionable notes instead of tracebacks.
    """

    opener = transport_opener or _default_transport_opener(baudrate)
    notes: list[str] = []
    best_report: ModemDiscoveryReport | None = None

    for port in candidate_ports:
        transport = opener(port)
        port_responses: dict[str, list[str]] = {}
        attention_ok = False
        opened = False
        try:
            await asyncio.wait_for(transport.open(), timeout=command_timeout)
            opened = True
            for command in SAFE_PROBE_COMMANDS:
                lines, status = await _send_safe_command(transport, command, command_timeout)
                port_responses[command] = lines
                if command == "AT" and status == "OK":
                    attention_ok = True
                if status == "timeout":
                    notes.append(
                        f"Port {port}: command {command} timed out; check that this is the modem AT port and that no other process is using it."
                    )
                    if command == "AT":
                        break
        except Exception as exc:  # noqa: BLE001 - user-facing note, not traceback
            notes.append(
                f"Port {port}: could not open or probe safely ({type(exc).__name__}); "
                "check permissions, port availability, and whether another process is using it."
            )
            continue
        finally:
            if opened:
                try:
                    await asyncio.wait_for(transport.close(), timeout=command_timeout)
                except Exception as exc:  # noqa: BLE001 - actionable cleanup note
                    notes.append(
                        f"Port {port}: probe completed, but closing the port failed ({type(exc).__name__})."
                    )

        identity = _identity_from_responses(port_responses)
        capabilities = classify_capabilities(identity)
        confidence = _confidence(identity, capabilities, attention_ok)
        if confidence == "no-response":
            continue

        report = ModemDiscoveryReport(
            at_port=port,
            audio_port=None,
            identity=identity,
            capabilities=capabilities,
            confidence=confidence,
            notes=(),
        )
        if best_report is None or report.confidence == "profile-match":
            best_report = report

    if best_report is not None:
        return replace(best_report, notes=tuple(notes) + profile_notes(best_report.identity))

    return ModemDiscoveryReport(
        at_port="",
        audio_port=None,
        identity=ModemIdentity(),
        capabilities=ModemCapabilities(),
        confidence="no-response",
        notes=tuple(notes)
        + (
            "No responsive modem AT port was found among the explicit candidate ports; verify the port list and permissions.",
        ),
    )
