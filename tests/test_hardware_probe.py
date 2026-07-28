"""Safe modem probe tests.

These tests use injectable fake transports only: no serial ports, no hardware, and no
sensitive identity commands.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from callstack.hardware.discovery import AudioPortHint
from callstack.hardware.probe import SAFE_PROBE_COMMANDS, discover_modems, probe_modem_ports
from callstack.transport.base import Transport


_INVALID_COMMAND_TIMEOUTS = (True, "0.5", None, float("nan"), float("inf"), float("-inf"), 0, -0.01)


class ScriptedTransport(Transport):
    def __init__(self, responses_by_command: dict[str, list[str]] | None = None, *, open_error: Exception | None = None):
        self.responses_by_command = responses_by_command or {}
        self.open_error = open_error
        self.opened = False
        self.closed = False
        self.writes: list[str] = []
        self._pending: list[bytes] = []

    async def open(self) -> None:
        if self.open_error is not None:
            raise self.open_error
        self.opened = True

    async def close(self) -> None:
        self.closed = True
        self.opened = False

    async def write(self, data: bytes) -> None:
        command = data.decode("ascii").strip()
        self.writes.append(command)
        self._pending = [f"{line}\r\n".encode("ascii") for line in self.responses_by_command.get(command, [])]

    async def readline(self) -> bytes:
        if self._pending:
            return self._pending.pop(0)
        await asyncio.sleep(60)
        return b""

    async def read(self, size: int = -1) -> bytes:
        return await self.readline()

    def in_waiting(self) -> int:
        return len(self._pending)


@pytest.mark.parametrize("command_timeout", _INVALID_COMMAND_TIMEOUTS)
async def test_probe_rejects_invalid_command_timeout_before_opening_transport(command_timeout: Any):
    opener_calls: list[str] = []

    def opener(port: str) -> Transport:
        opener_calls.append(port)
        raise AssertionError("invalid timeout must not open a transport")

    with pytest.raises(ValueError):
        await probe_modem_ports(["/dev/fake-at"], transport_opener=opener, command_timeout=command_timeout)

    assert opener_calls == []


@pytest.mark.parametrize("command_timeout", _INVALID_COMMAND_TIMEOUTS)
async def test_discovery_rejects_invalid_command_timeout_before_enumerating_candidates(command_timeout: Any):
    glob_calls: list[str] = []
    opener_calls: list[str] = []

    def globber(pattern: str) -> list[str]:
        glob_calls.append(pattern)
        raise AssertionError("invalid timeout must not enumerate candidates")

    def opener(port: str) -> Transport:
        opener_calls.append(port)
        raise AssertionError("invalid timeout must not open a transport")

    with pytest.raises(ValueError):
        await discover_modems(
            patterns="/dev/fake*",
            path_glob=globber,
            transport_opener=opener,
            command_timeout=command_timeout,
        )

    assert glob_calls == []
    assert opener_calls == []


async def test_probe_sends_only_safe_commands_returns_simcom_report_and_closes_transport():
    transport = ScriptedTransport(
        {
            "AT": ["AT", "OK"],
            "ATI": ["SIMCOM INCORPORATED", "SIMCOM_SIM7600E-H", "Revision: LE20B01SIM7600M22", "OK"],
            "AT+GMI": ["SIMCOM INCORPORATED", "OK"],
            "AT+GMM": ["SIMCOM_SIM7600E-H", "OK"],
            "AT+GMR": ["LE20B01SIM7600M22", "OK"],
        }
    )

    report = await probe_modem_ports(
        ["/dev/fake-at"],
        transport_opener=lambda port: transport,
        command_timeout=0.01,
    )

    assert transport.opened is False
    assert transport.closed is True
    assert tuple(transport.writes) == SAFE_PROBE_COMMANDS
    assert report.at_port == "/dev/fake-at"
    assert report.audio_port is None
    assert report.identity.manufacturer == "SIMCOM INCORPORATED"
    assert report.identity.model == "SIMCOM_SIM7600E-H"
    assert report.identity.revision == "LE20B01SIM7600M22"
    assert report.confidence == "profile-match"
    assert report.capabilities.sms_text_mode == "supported"
    assert report.capabilities.voice_calls == "supported"
    assert any("SIMCom" in note or "SIMCOM" in note for note in report.notes)

    unsafe_terms = ("imei", "imsi", "iccid", "cnum", "+gsn", "+cimi", "+ccid")
    assert all(term not in " ".join(transport.writes).lower() for term in unsafe_terms)


async def test_probe_records_open_failures_and_timeouts_as_actionable_notes_without_tracebacks():
    permission_denied = ScriptedTransport(open_error=PermissionError("permission denied"))
    timeout = ScriptedTransport({"AT": ["AT"]})
    good = ScriptedTransport(
        {
            "AT": ["OK"],
            "ATI": ["Quectel", "EC25", "OK"],
            "AT+GMI": ["Quectel", "OK"],
            "AT+GMM": ["EC25", "OK"],
            "AT+GMR": ["EC25EFAR06A08M4G", "OK"],
        }
    )
    transports = {
        "/dev/no-permission": permission_denied,
        "/dev/timeout": timeout,
        "/dev/good": good,
    }

    report = await probe_modem_ports(
        list(transports),
        transport_opener=lambda port: transports[port],
        command_timeout=0.001,
    )

    assert timeout.closed is True
    assert good.closed is True
    assert report.at_port == "/dev/good"
    assert report.identity.manufacturer == "Quectel"
    assert report.identity.model == "EC25"
    assert report.capabilities.ussd == "supported"
    combined_notes = "\n".join(report.notes)
    assert "/dev/no-permission" in combined_notes
    assert "permissions" in combined_notes.lower()
    assert "PRIVATE_DEVICE_DETAIL" not in combined_notes
    assert "/dev/timeout" in combined_notes
    assert "timed out" in combined_notes.lower()
    assert "Traceback" not in combined_notes


async def test_probe_redacts_sensitive_ati_identifier_lines_and_records_presence_boolean():
    sensitive_imei = "123456789012345"
    transport = ScriptedTransport(
        {
            "AT": ["OK"],
            "ATI": ["SIMCOM INCORPORATED", "SIMCOM_SIM7600E-H", sensitive_imei, "OK"],
            "AT+GMI": ["SIMCOM INCORPORATED", "OK"],
            "AT+GMM": ["SIMCOM_SIM7600E-H", "OK"],
            "AT+GMR": ["ERROR"],
        }
    )

    report = await probe_modem_ports(
        ["/dev/fake-at"],
        transport_opener=lambda port: transport,
        command_timeout=0.01,
    )

    combined_report = f"{report.identity!r} {' '.join(report.notes)}"
    assert sensitive_imei not in combined_report
    assert report.identity.imei_present is True
    assert report.identity.revision == ""


async def test_probe_redacts_common_identifier_labels_and_long_iccid_like_numbers():
    sensitive_gsn = "+GSN: 123456789012345"
    sensitive_iccid = "89012345678901234567"
    transport = ScriptedTransport(
        {
            "AT": ["OK"],
            "ATI": ["Maker", "ModelX", sensitive_gsn, sensitive_iccid, "OK"],
            "AT+GMI": ["ERROR"],
            "AT+GMM": ["ERROR"],
            "AT+GMR": ["ERROR"],
        }
    )

    report = await probe_modem_ports(
        ["/dev/fake-at"],
        transport_opener=lambda port: transport,
        command_timeout=0.01,
    )

    combined_report = f"{report.identity!r} {' '.join(report.notes)}"
    assert sensitive_gsn not in combined_report
    assert sensitive_iccid not in combined_report
    assert report.identity.imei_present is True
    assert report.identity.manufacturer == "Maker"
    assert report.identity.model == "ModelX"
    assert report.identity.revision == ""


async def test_probe_applies_timeout_to_total_command_exchange_not_each_line_only():
    class SlowLineTransport(ScriptedTransport):
        async def readline(self) -> bytes:
            if self._pending:
                await asyncio.sleep(0.003)
                return self._pending.pop(0)
            await asyncio.sleep(60)
            return b""

    transport = SlowLineTransport({"AT": ["AT", "OK"]})

    report = await probe_modem_ports(
        ["/dev/slow"],
        transport_opener=lambda port: transport,
        command_timeout=0.005,
    )

    assert report.confidence == "no-response"
    assert any("timed out" in note.lower() for note in report.notes)


async def test_probe_redacts_exception_details_from_failure_notes():
    sensitive_detail = "PRIVATE_DEVICE_DETAIL"
    transport = ScriptedTransport(open_error=PermissionError(f"permission denied for {sensitive_detail}"))

    report = await probe_modem_ports(
        ["/dev/private"],
        transport_opener=lambda port: transport,
        command_timeout=0.001,
    )

    combined_notes = "\n".join(report.notes)
    assert "/dev/private" in combined_notes
    assert "PermissionError" in combined_notes
    assert sensitive_detail not in combined_notes
    assert "Traceback" not in combined_notes


async def test_probe_returns_pii_safe_unknown_report_when_no_candidate_responds():
    transport = ScriptedTransport({"AT": ["AT"]})

    report = await probe_modem_ports(
        ["/dev/silent"],
        transport_opener=lambda port: transport,
        command_timeout=0.001,
    )

    assert report.at_port == ""
    assert report.audio_port is None
    assert report.confidence == "no-response"
    assert report.identity.manufacturer == ""
    assert set(report.capabilities.__dict__.values()) == {"unknown"}
    assert any("no responsive modem" in note.lower() for note in report.notes)


async def test_discover_modems_uses_injected_patterns_and_returns_safe_scan_report():
    """Opt-in host scans enumerate candidates through an injectable globber."""
    no_response = ScriptedTransport({"AT": ["AT"]})
    good = ScriptedTransport(
        {
            "AT": ["OK"],
            "ATI": ["Quectel", "EC25", "OK"],
            "AT+GMI": ["Quectel", "OK"],
            "AT+GMM": ["EC25", "OK"],
            "AT+GMR": ["EC25EFAR06A08M4G", "OK"],
        }
    )
    transports = {
        "/dev/ttyUSB0": no_response,
        "/dev/ttyUSB1": good,
    }
    glob_calls = []

    def fake_glob(pattern: str) -> list[str]:
        glob_calls.append(pattern)
        if pattern == "/dev/ttyUSB*":
            return ["/dev/ttyUSB1", "/dev/ttyUSB0"]
        if pattern == "/dev/ttyACM*":
            return ["/dev/ttyUSB1"]
        raise AssertionError(f"unexpected pattern: {pattern}")

    reports = await discover_modems(
        patterns=("/dev/ttyUSB*", "/dev/ttyACM*"),
        path_glob=fake_glob,
        transport_opener=lambda port: transports[port],
        command_timeout=0.001,
    )

    assert glob_calls == ["/dev/ttyUSB*", "/dev/ttyACM*"]
    assert len(reports) == 1
    report = reports[0]
    assert report.at_port == "/dev/ttyUSB1"
    assert report.audio_port is None
    assert report.identity.manufacturer == "Quectel"
    assert report.capabilities.ussd == "supported"
    assert any("2 candidate" in note for note in report.notes)
    assert no_response.closed is True
    assert good.closed is True
    assert tuple(good.writes) == SAFE_PROBE_COMMANDS


async def test_discover_modems_reports_when_scan_patterns_match_no_ports():
    reports = await discover_modems(
        patterns=("/tmp/no-modems*",),
        path_glob=lambda pattern: [],
        command_timeout=0.001,
    )

    assert len(reports) == 1
    report = reports[0]
    assert report.at_port == ""
    assert report.confidence == "no-response"
    assert any("no candidate" in note.lower() for note in report.notes)


async def test_probe_ranks_identity_response_above_attention_only_candidate():
    attention_only = ScriptedTransport(
        {
            "AT": ["OK"],
            "ATI": ["ERROR"],
            "AT+GMI": ["ERROR"],
            "AT+GMM": ["ERROR"],
            "AT+GMR": ["ERROR"],
        }
    )
    identity = ScriptedTransport(
        {
            "AT": ["OK"],
            "ATI": ["MysteryVendor", "MysteryModel", "OK"],
            "AT+GMI": ["MysteryVendor", "OK"],
            "AT+GMM": ["MysteryModel", "OK"],
            "AT+GMR": ["ERROR"],
        }
    )
    transports = {
        "/dev/attention": attention_only,
        "/dev/identity": identity,
    }

    report = await probe_modem_ports(
        ["/dev/attention", "/dev/identity"],
        transport_opener=lambda port: transports[port],
        command_timeout=0.001,
    )

    assert report.at_port == "/dev/identity"
    assert report.confidence == "identity-response"


async def test_discover_modems_accepts_single_pattern_string_as_one_glob():
    good = ScriptedTransport({"AT": ["OK"]})
    glob_calls = []

    def fake_glob(pattern: str) -> list[str]:
        glob_calls.append(pattern)
        return ["/dev/ttyUSB0"]

    reports = await discover_modems(
        patterns="/dev/ttyUSB*",
        path_glob=fake_glob,
        transport_opener=lambda port: good,
        command_timeout=0.001,
    )

    assert glob_calls == ["/dev/ttyUSB*"]
    assert reports[0].at_port == "/dev/ttyUSB0"


async def test_probe_surfaces_explicit_audio_port_as_configured_hint_without_extra_commands():
    transport = ScriptedTransport({"AT": ["OK"], "ATI": ["Quectel", "EC25", "OK"]})

    report = await probe_modem_ports(
        ["/dev/ttyUSB2"],
        transport_opener=lambda port: transport,
        configured_audio_port="/dev/ttyUSB4",
        command_timeout=0.001,
    )

    assert report.audio_port == "/dev/ttyUSB4"
    assert report.audio_hint == AudioPortHint(
        port="/dev/ttyUSB4",
        confidence="configured",
        reason="Operator configured CALLSTACK_AUDIO_PORT explicitly; discovery did not verify the audio role.",
    )
    assert tuple(transport.writes) == SAFE_PROBE_COMMANDS


async def test_discover_modems_reports_ambiguous_sibling_serial_audio_hint_without_selecting_port():
    good = ScriptedTransport({"AT": ["OK"], "ATI": ["MysteryVendor", "MysteryModel", "OK"]})
    quiet = ScriptedTransport({"AT": ["AT"]})
    transports = {
        "/dev/ttyUSB0": good,
        "/dev/ttyUSB1": quiet,
        "/dev/ttyUSB2": quiet,
    }

    reports = await discover_modems(
        patterns="/dev/ttyUSB*",
        path_glob=lambda pattern: tuple(transports),
        transport_opener=lambda port: transports[port],
        command_timeout=0.001,
    )

    report = reports[0]
    assert report.at_port == "/dev/ttyUSB0"
    assert report.audio_port is None
    assert report.audio_hint.port is None
    assert report.audio_hint.confidence == "sibling-serial"
    assert "audio role cannot be proven safely" in report.audio_hint.reason.lower()
    assert "audio hint confidence: sibling-serial" in report.notes[0]
    assert any("CALLSTACK_AUDIO_PORT manually" in note for note in report.notes)


async def test_discover_modems_scan_note_reflects_profile_audio_hint_without_claiming_configured_port():
    """Profile hints should not be contradicted by the opt-in scan summary note."""
    simcom = ScriptedTransport(
        {
            "AT": ["OK"],
            "ATI": ["SIMCOM INCORPORATED", "SIMCOM_SIM7600E-H", "OK"],
            "AT+GMI": ["SIMCOM INCORPORATED", "OK"],
            "AT+GMM": ["SIMCOM_SIM7600E-H", "OK"],
            "AT+GMR": ["LE20B01SIM7600M22", "OK"],
        }
    )

    reports = await discover_modems(
        patterns="/dev/ttyUSB*",
        path_glob=lambda pattern: ["/dev/ttyUSB2"],
        transport_opener=lambda port: simcom,
        command_timeout=0.001,
    )

    report = reports[0]
    assert report.audio_hint.port is None
    assert report.audio_hint.confidence == "profile-hint"
    scan_note = report.notes[0]
    assert "audio hint confidence: profile-hint" in scan_note
    assert "audio port remains unknown unless configured explicitly" not in scan_note
    assert report.audio_port is None


async def test_discover_modems_surfaces_sim7600_plus_two_sibling_only_as_unconfigured_profile_hint():
    sim7600 = ScriptedTransport(
        {
            "AT": ["OK"],
            "ATI": ["SIMCOM INCORPORATED", "SIMCOM_SIM7600E-H", "OK"],
            "AT+GMI": ["SIMCOM INCORPORATED", "OK"],
            "AT+GMM": ["SIMCOM_SIM7600E-H", "OK"],
            "AT+GMR": ["LE20B01SIM7600M22", "OK"],
        }
    )
    quiet = ScriptedTransport({"AT": ["AT"]})
    transports = {
        "/dev/ttyUSB2": sim7600,
        "/dev/ttyUSB4": quiet,
    }

    reports = await discover_modems(
        patterns="/dev/ttyUSB*",
        path_glob=lambda pattern: tuple(transports),
        transport_opener=lambda port: transports[port],
        command_timeout=0.001,
    )

    report = reports[0]
    assert report.at_port == "/dev/ttyUSB2"
    assert report.audio_port is None
    assert report.audio_hint.port == "/dev/ttyUSB4"
    assert report.audio_hint.confidence == "profile-hint"
    assert tuple(sim7600.writes) == SAFE_PROBE_COMMANDS
    assert tuple(quiet.writes) == ("AT",)


class EOFTransport(ScriptedTransport):
    """Fake transport simulating a closed/disconnected stream: readline returns b"" immediately."""

    def __init__(self):
        super().__init__()
        self.readline_calls = 0

    async def readline(self) -> bytes:
        self.readline_calls += 1
        return b""


async def test_probe_handles_transport_eof_without_hanging_and_prefers_later_responsive_candidate():
    eof_transport = EOFTransport()
    timeout_transport = ScriptedTransport({"AT": ["AT"]})
    good_transport = ScriptedTransport(
        {
            "AT": ["OK"],
            "ATI": ["Quectel", "EC25", "OK"],
            "AT+GMI": ["Quectel", "OK"],
            "AT+GMM": ["EC25", "OK"],
            "AT+GMR": ["EC25EFAR06A08M4G", "OK"],
        }
    )
    transports = {
        "/dev/eof": eof_transport,
        "/dev/timeout": timeout_transport,
        "/dev/good": good_transport,
    }

    report = await probe_modem_ports(
        ["/dev/eof", "/dev/timeout", "/dev/good"],
        transport_opener=lambda port: transports[port],
        command_timeout=0.01,
    )

    # EOF candidate: exactly one read attempt for its AT command, then closed and skipped.
    assert eof_transport.writes == ["AT"]
    assert eof_transport.readline_calls == 1
    assert eof_transport.closed is True

    # The later responsive candidate is still selected as the best report.
    assert report.at_port == "/dev/good"
    assert report.identity.manufacturer == "Quectel"
    assert report.identity.model == "EC25"

    eof_note = next(note for note in report.notes if "/dev/eof" in note)
    timeout_note = next(note for note in report.notes if "/dev/timeout" in note)

    # Generic, PII-safe EOF/disconnect note distinct from the timeout note.
    assert "closed the connection" in eof_note.lower() or "eof" in eof_note.lower()
    assert "timed out" not in eof_note.lower()

    # Existing silent-but-open timeout behavior remains intact and distinct.
    assert "timed out" in timeout_note.lower()

    # No custom exception class names or private transport details leak into notes.
    combined_notes = "\n".join(report.notes)
    assert "EOFTransport" not in combined_notes
    assert "ScriptedTransport" not in combined_notes
    assert "Exception" not in combined_notes
    assert "Traceback" not in combined_notes


async def test_sim7600_profile_hint_does_not_override_explicit_configured_audio_port():
    sim7600 = ScriptedTransport(
        {
            "AT": ["OK"],
            "ATI": ["SIMCOM INCORPORATED", "SIMCOM_SIM7600E-H", "OK"],
            "AT+GMI": ["SIMCOM INCORPORATED", "OK"],
            "AT+GMM": ["SIMCOM_SIM7600E-H", "OK"],
            "AT+GMR": ["LE20B01SIM7600M22", "OK"],
        }
    )

    report = await probe_modem_ports(
        ["/dev/ttyUSB2", "/dev/ttyUSB4"],
        transport_opener=lambda port: sim7600,
        configured_audio_port="/dev/configured-audio",
        command_timeout=0.001,
    )

    assert report.audio_port == "/dev/configured-audio"
    assert report.audio_hint.confidence == "configured"
    assert report.audio_hint.port == "/dev/configured-audio"
