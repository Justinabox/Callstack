"""Pure hardware profile classification tests.

These tests intentionally exercise only deterministic dataclasses/helpers: no serial
ports, no pyserial opens, and no AT probing.
"""

from dataclasses import asdict, fields
from typing import Any, cast

import pytest

from callstack.hardware.discovery import (
    ModemCapabilities,
    ModemDiscoveryReport,
    ModemIdentity,
)
from callstack.hardware.profiles import classify_capabilities, profile_notes


LOW_RISK_SIMCOM_CAPABILITIES = {
    "sms_text_mode",
    "sms_pdu_mode",
    "delivery_reports",
    "ussd",
    "voice_calls",
    "dtmf_send",
}

QUECTEL_SMS_USSD_CAPABILITIES = {
    "sms_text_mode",
    "sms_pdu_mode",
    "delivery_reports",
    "ussd",
}


class TestModemIdentity:
    def test_defaults_are_empty_and_do_not_store_sensitive_identifiers(self):
        identity = ModemIdentity()

        assert identity.manufacturer == ""
        assert identity.model == ""
        assert identity.revision == ""
        assert identity.imei_present is False

        field_names = {field.name for field in fields(identity)}
        assert "imei" not in field_names
        assert "imsi" not in field_names
        assert "iccid" not in field_names
        assert not hasattr(identity, "imei")
        assert not hasattr(identity, "imsi")

    def test_imei_presence_is_recorded_as_boolean_only(self):
        redacted_identifier_sentinel = "REDACTED_TEST_IDENTIFIER"
        identity = ModemIdentity(
            manufacturer="SIMCOM INCORPORATED",
            model="SIMCOM_SIM7600E-H",
            revision="LE20B01SIM7600M22",
            imei_present=True,
        )

        assert identity.imei_present is True
        assert redacted_identifier_sentinel not in repr(identity)
        assert redacted_identifier_sentinel not in str(asdict(identity))


class TestModemCapabilities:
    def test_all_capabilities_default_to_unknown(self):
        capabilities = ModemCapabilities()

        assert asdict(capabilities) == {
            "sms_text_mode": "unknown",
            "sms_pdu_mode": "unknown",
            "delivery_reports": "unknown",
            "ussd": "unknown",
            "voice_calls": "unknown",
            "dtmf_send": "unknown",
            "pcm_audio": "unknown",
            "gnss": "unknown",
        }

    def test_invalid_capability_status_is_rejected(self):
        with pytest.raises(ValueError, match="sms_text_mode"):
            ModemCapabilities(sms_text_mode=cast(Any, "maybe"))


class TestModemDiscoveryReport:
    def test_report_carries_ports_identity_capabilities_confidence_and_notes(self):
        identity = ModemIdentity(manufacturer="Quectel", model="EC25")
        capabilities = ModemCapabilities(sms_text_mode="supported")
        report = ModemDiscoveryReport(
            at_port="/tmp/fake-at-port",
            audio_port="/tmp/fake-audio-port",
            identity=identity,
            capabilities=capabilities,
            confidence="profile-match",
            notes=("pure profile classification only",),
        )

        assert report.at_port == "/tmp/fake-at-port"
        assert report.audio_port == "/tmp/fake-audio-port"
        assert report.identity is identity
        assert report.capabilities is capabilities
        assert report.confidence == "profile-match"
        assert report.notes == ("pure profile classification only",)

    def test_audio_port_is_optional(self):
        report = ModemDiscoveryReport(at_port="/tmp/fake-at-port")

        assert report.audio_port is None
        assert report.identity == ModemIdentity()
        assert report.capabilities == ModemCapabilities()
        assert report.confidence == "unknown"
        assert report.notes == ()


class TestClassifyCapabilities:
    def test_simcom_like_identity_marks_only_low_risk_capabilities_supported(self):
        identity = ModemIdentity(
            manufacturer="SIMCOM INCORPORATED",
            model="SIMCOM_SIM7600E-H",
            revision="LE20B01SIM7600M22",
            imei_present=True,
        )

        capabilities = classify_capabilities(identity)

        values = asdict(capabilities)
        for name in LOW_RISK_SIMCOM_CAPABILITIES:
            assert values[name] == "supported"
        assert values["pcm_audio"] == "unknown"
        assert values["gnss"] == "unknown"

    def test_quectel_like_identity_marks_sms_and_ussd_supported_conservatively(self):
        identity = ModemIdentity(manufacturer="Quectel", model="EC25", revision="EC25EFAR06A08M4G")

        capabilities = classify_capabilities(identity)

        values = asdict(capabilities)
        for name in QUECTEL_SMS_USSD_CAPABILITIES:
            assert values[name] == "supported"
        assert values["voice_calls"] == "unknown"
        assert values["dtmf_send"] == "unknown"
        assert values["pcm_audio"] == "unknown"
        assert values["gnss"] == "unknown"

    def test_unknown_identity_returns_all_unknown_capabilities_and_actionable_note(self):
        identity = ModemIdentity(manufacturer="MysteryVendor", model="MysteryBox")

        capabilities = classify_capabilities(identity)
        notes = profile_notes(identity)

        assert set(asdict(capabilities).values()) == {"unknown"}
        assert notes
        assert any("unknown" in note.lower() for note in notes)
        assert any("probe" in note.lower() or "manual" in note.lower() for note in notes)

    def test_profile_helpers_do_not_expose_sensitive_identifier_fields(self):
        identity = ModemIdentity(manufacturer="Quectel", model="EC25", imei_present=True)

        capabilities = classify_capabilities(identity)
        notes = profile_notes(identity)

        sensitive_terms = ("imei=", "imsi", "iccid")
        combined = f"{identity!r} {capabilities!r} {' '.join(notes)}".lower()
        assert all(term not in combined for term in sensitive_terms)
