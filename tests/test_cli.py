"""Tests for the installable Callstack CLI."""

import asyncio
import json
from dataclasses import asdict
from datetime import datetime, timezone

from callstack.config import ModemConfig
from callstack.errors import SMSSendError
from callstack.hardware.discovery import ModemDiscoveryReport, ModemIdentity
from callstack.hardware.profiles import classify_capabilities
from callstack.network import RegistrationInfo, SignalInfo
from callstack.sms.store import SMSStore
from callstack.sms.types import SMS


class FakeNetwork:
    async def registration(self):
        return RegistrationInfo(status=1, mode=0)

    async def signal_quality(self):
        return SignalInfo(
            rssi=18,
            ber=2,
            dbm=-77,
            description="good",
            ber_description="good",
        )

    async def operator(self):
        return "ExampleCarrier"


class FakeSMS:
    def __init__(self, modem):
        self._modem = modem

    async def send(self, to, body):
        self._modem.sent.append((to, body))
        return SMS(recipient=to, body=body, status="sent", reference=42)


class FakeModem:
    instances = []

    def __init__(self, config: ModemConfig):
        self.config = config
        self.network = FakeNetwork()
        self.sms = FakeSMS(self)
        self.sent = []
        self.closed = False
        FakeModem.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return None


def _install_fake_modem(monkeypatch):
    import callstack.cli as cli

    FakeModem.instances.clear()
    monkeypatch.setattr(cli, "Modem", FakeModem)
    return cli


def test_status_json_outputs_network_snapshot_and_maps_config_flags(monkeypatch, capsys):
    monkeypatch.setenv("CALLSTACK_TEST_SIM_PIN", "1234")
    cli = _install_fake_modem(monkeypatch)

    code = cli.main(
        [
            "--at-port",
            "/dev/testAT",
            "--audio-port",
            "/dev/testAudio",
            "--baudrate",
            "9600",
            "--sms-db-path",
            "/tmp/callstack.sqlite",
            "--sim-pin-env",
            "CALLSTACK_TEST_SIM_PIN",
            "--log-level",
            "DEBUG",
            "status",
            "--json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "connected": True,
        "registration": {
            "registered": True,
            "roaming": False,
            "description": "registered (home)",
        },
        "signal": {
            "rssi": 18,
            "dbm": -77,
            "description": "good",
            "ber": 2,
            "ber_description": "good",
        },
        "operator": "ExampleCarrier",
    }
    config = FakeModem.instances[0].config
    assert config.at_port == "/dev/testAT"
    assert config.audio_port == "/dev/testAudio"
    assert config.baudrate == 9600
    assert config.sms_db_path == "/tmp/callstack.sqlite"
    assert config.sim_pin == "1234"
    assert config.log_level == "DEBUG"


def test_status_human_output_handles_unknown_values(monkeypatch, capsys):
    import callstack.cli as cli

    class UnknownNetwork:
        async def registration(self):
            return RegistrationInfo(status=0, mode=0)

        async def signal_quality(self):
            return SignalInfo(
                rssi=99,
                ber=99,
                dbm=None,
                description="unknown",
                ber_description="unknown",
            )

        async def operator(self):
            return None

    class UnknownModem(FakeModem):
        def __init__(self, config):
            super().__init__(config)
            self.network = UnknownNetwork()

    monkeypatch.setattr(cli, "Modem", UnknownModem)

    code = cli.main(["status"])

    assert code == 0
    output = capsys.readouterr().out
    assert "Modem: connected" in output
    assert "Registration: not registered" in output
    assert "Operator: unknown" in output
    assert "Signal: unknown" in output


def test_send_success_calls_sms_once_and_masks_private_content(monkeypatch, capsys):
    cli = _install_fake_modem(monkeypatch)

    code = cli.main(["send", "--to", "+155****4567", "--body", "secret passcode"])

    assert code == 0
    assert FakeModem.instances[0].sent == [("+155****4567", "secret passcode")]
    assert FakeModem.instances[0].closed is True
    output = capsys.readouterr().out
    assert "SMS sent" in output
    assert "ref: 42" in output
    assert "+15551234567" not in output
    assert "secret passcode" not in output


def test_send_failure_returns_nonzero_without_traceback_or_private_fields(monkeypatch, capsys):
    import callstack.cli as cli

    class FailingSMS:
        async def send(self, to, body):
            raise SMSSendError(f"carrier rejected {to}: {body}")

    class FailingModem(FakeModem):
        def __init__(self, config):
            super().__init__(config)
            self.sms = FailingSMS()

    monkeypatch.setattr(cli, "Modem", FailingModem)

    code = cli.main(["send", "--to", "+15551234567", "--body", "secret passcode"])

    captured = capsys.readouterr()
    assert code == 1
    assert "Error:" in captured.err
    assert "SMSSendError" in captured.err
    assert "Traceback" not in captured.err
    assert "+15551234567" not in captured.err
    assert "secret passcode" not in captured.err


def test_status_accepts_config_flags_after_subcommand(monkeypatch, capsys):
    cli = _install_fake_modem(monkeypatch)

    code = cli.main(["status", "--at-port", "/dev/subAT", "--json"])

    assert code == 0
    json.loads(capsys.readouterr().out)
    assert FakeModem.instances[0].config.at_port == "/dev/subAT"


def test_help_includes_status_and_send_commands(capsys):
    import callstack.cli as cli

    code = cli.main(["--help"])

    assert code == 0
    output = capsys.readouterr().out
    assert "status" in output
    assert "send" in output
    assert "doctor" in output
    assert "inbox" in output
    assert "--at-port" in output


def test_subcommand_help_includes_command_and_config_flags(capsys):
    import callstack.cli as cli

    status_code = cli.main(["status", "--help"])
    status_output = capsys.readouterr().out
    send_code = cli.main(["send", "--help"])
    send_output = capsys.readouterr().out

    assert status_code == 0
    assert "--json" in status_output
    assert "--at-port" in status_output
    assert send_code == 0
    assert "--to" in send_output
    assert "--body" in send_output
    assert "--at-port" in send_output


def test_doctor_defaults_to_configured_at_port_when_ports_are_omitted(monkeypatch, capsys):
    import callstack.cli as cli

    called = {}
    report = ModemDiscoveryReport(at_port="/dev/ttyUSB2")

    async def fake_probe(ports, **kwargs):
        called["ports"] = ports
        return report

    monkeypatch.setattr(cli, "probe_modem_ports", fake_probe)

    code = cli.main(["doctor", "--json"])

    assert code == 0
    assert called == {"ports": ["/dev/ttyUSB2"]}
    assert json.loads(capsys.readouterr().out)["at_port"] == "/dev/ttyUSB2"


def test_doctor_passes_configured_baudrate_to_probe(monkeypatch, capsys):
    import callstack.cli as cli

    called = {}
    report = ModemDiscoveryReport(at_port="/dev/subAT")

    async def fake_probe(ports, **kwargs):
        called["ports"] = ports
        called.update(kwargs)
        return report

    monkeypatch.setattr(cli, "probe_modem_ports", fake_probe)

    code = cli.main(["doctor", "--at-port", "/dev/subAT", "--baudrate", "9600", "--json"])

    assert code == 0
    assert called["ports"] == ["/dev/subAT"]
    assert called["baudrate"] == 9600
    json.loads(capsys.readouterr().out)


def test_doctor_json_uses_probe_and_serializes_pii_safe_report(monkeypatch, capsys):
    import callstack.cli as cli

    sentinel = "SENSITIVE_SENTINEL_SHOULD_NOT_APPEAR"
    called = {}
    report = ModemDiscoveryReport(
        at_port="/dev/fakeAT",
        audio_port=None,
        identity=ModemIdentity(
            manufacturer="SIMCOM INCORPORATED",
            model="SIMCOM_SIM7600E-H",
            revision="LE20B01SIM7600M22",
        ),
        confidence="profile-match",
        notes=("SIMCom profile matched", f"redacted marker omitted: {sentinel}".replace(sentinel, "[redacted]")),
    )

    async def fake_probe(ports, **kwargs):
        called["ports"] = ports
        return report

    monkeypatch.setattr(cli, "probe_modem_ports", fake_probe)

    code = cli.main(["doctor", "--ports", "/dev/fakeAT,/dev/other", "--json"])

    assert code == 0
    assert called == {"ports": ["/dev/fakeAT", "/dev/other"]}
    output = capsys.readouterr().out
    assert sentinel not in output
    payload = json.loads(output)
    assert payload == {
        "at_port": "/dev/fakeAT",
        "audio_port": None,
        "identity": asdict(report.identity),
        "capabilities": asdict(report.capabilities),
        "confidence": "profile-match",
        "notes": list(report.notes),
    }


def test_doctor_human_output_includes_safe_summary_capabilities_notes_and_safety_text(monkeypatch, capsys):
    import callstack.cli as cli

    identity = ModemIdentity(manufacturer="Quectel", model="EC25", revision="EC25EFAR06A08M4G")
    report = ModemDiscoveryReport(
        at_port="/dev/fakeAT",
        identity=identity,
        capabilities=classify_capabilities(identity),
        confidence="profile-match",
        notes=("Quectel-like identity matched", "Audio probing is intentionally not performed."),
    )

    async def fake_probe(ports, **kwargs):
        assert ports == ["/dev/fakeAT"]
        return report

    monkeypatch.setattr(cli, "probe_modem_ports", fake_probe)

    code = cli.main(["doctor", "--ports", "/dev/fakeAT"])

    assert code == 0
    output = capsys.readouterr().out
    assert "AT port: /dev/fakeAT (confidence: profile-match)" in output
    assert "Audio port: unknown" in output
    assert "Manufacturer: Quectel" in output
    assert "Model: EC25" in output
    assert "sms_text_mode: supported" in output
    assert "voice_calls: unknown" in output
    assert "Quectel-like identity matched" in output
    assert "no SMS, USSD, call, SIM unlock, or storage commands were sent" in output


def test_send_defaults_to_warning_logging_to_avoid_private_info_logs(monkeypatch, capsys):
    cli = _install_fake_modem(monkeypatch)

    code = cli.main(["send", "--to", "+155****4567", "--body", "secret passcode"])

    assert code == 0
    assert FakeModem.instances[0].config.log_level == "WARNING"
    capsys.readouterr()


def _seed_sms_db(db_path, messages):
    async def seed():
        store = SMSStore(db_path=str(db_path))
        try:
            await store.initialize()
            for message in messages:
                await store.save(message)
        finally:
            await store.close()

    asyncio.run(seed())


def test_inbox_json_reads_bounded_persisted_messages_newest_first_without_modem(tmp_path, monkeypatch, capsys):
    import callstack.cli as cli

    class ExplodingModem:
        def __init__(self, *args, **kwargs):
            raise AssertionError("inbox must not instantiate a modem")

    db_path = tmp_path / "sms.sqlite"
    _seed_sms_db(
        db_path,
        [
            SMS(sender="+155****0001", body="old", status="unread", timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc), storage_index=1),
            SMS(sender="+155****0002", body="middle", status="read", timestamp=datetime(2026, 1, 2, tzinfo=timezone.utc), storage_index=2),
            SMS(sender="+155****0003", body="new", status="unread", timestamp=datetime(2026, 1, 3, tzinfo=timezone.utc), storage_index=3),
        ],
    )
    monkeypatch.setattr(cli, "Modem", ExplodingModem)

    code = cli.main(["inbox", "--sms-db-path", str(db_path), "--json", "--limit", "2"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [message["body"] for message in payload] == ["new", "middle"]
    assert payload[0] == {
        "id": 3,
        "sender": "+155****0003",
        "body": "new",
        "timestamp": "2026-01-03T00:00:00+00:00",
        "status": "unread",
        "storage_index": 3,
    }


def test_inbox_filters_sender_and_status_before_json_limit(tmp_path, capsys):
    import callstack.cli as cli

    db_path = tmp_path / "sms.sqlite"
    _seed_sms_db(
        db_path,
        [
            SMS(sender="+155****0001", body="skip sender", status="unread"),
            SMS(sender="+155****0002", body="skip status", status="read"),
            SMS(sender="+155****0002", body="match one", status="unread"),
            SMS(sender="+155****0002", body="match two", status="unread"),
        ],
    )

    code = cli.main(
        [
            "inbox",
            "--sms-db-path",
            str(db_path),
            "--json",
            "--sender",
            "+155****0002",
            "--status",
            "unread",
            "--limit",
            "1",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [message["body"] for message in payload] == ["match two"]


def test_inbox_human_output_is_bounded_and_single_line(tmp_path, capsys):
    import callstack.cli as cli

    db_path = tmp_path / "sms.sqlite"
    _seed_sms_db(
        db_path,
        [
            SMS(sender="+155****0001", body="old message", status="read"),
            SMS(sender="+155****0002", body="line one\nline two", status="unread"),
        ],
    )

    code = cli.main(["inbox", "--sms-db-path", str(db_path), "--limit", "1"])

    assert code == 0
    output = capsys.readouterr().out
    assert "line one line two" in output
    assert "old message" not in output


def test_inbox_human_output_escapes_terminal_control_characters(tmp_path, capsys):
    import callstack.cli as cli

    db_path = tmp_path / "sms.sqlite"
    _seed_sms_db(
        db_path,
        [SMS(sender="+155****0002\x1b]52", body="line\x1b[2Jbody", status="unread\x1b")],
    )

    code = cli.main(["inbox", "--sms-db-path", str(db_path), "--limit", "1"])

    assert code == 0
    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert "]52" in output
    assert "line?[2Jbody" in output


def test_inbox_parse_errors_do_not_echo_private_argument_values(capsys):
    import callstack.cli as cli

    code = cli.main(["inbox", "--limit", "secret passcode", "--status", "unread"])

    captured = capsys.readouterr()
    assert code == 2
    assert "Error:" in captured.err
    assert "secret passcode" not in captured.err
    assert "Traceback" not in captured.err


def test_inbox_missing_sqlite_support_fails_closed_without_false_empty_result(monkeypatch, capsys):
    import callstack.cli as cli

    class UnavailableSQLiteStore:
        def __init__(self, db_path=None):
            self._db = None

        async def initialize(self, **kwargs):
            return None

        async def list(self, **kwargs):
            return []

        async def close(self):
            pass

    monkeypatch.setattr(cli, "SMSStore", UnavailableSQLiteStore)

    code = cli.main(["inbox", "--sms-db-path", "/tmp/sms.sqlite", "--json"])

    captured = capsys.readouterr()
    assert code == 1
    assert "Error:" in captured.err
    assert "[]" not in captured.out
    assert "Traceback" not in captured.err


def test_inbox_failure_returns_nonzero_without_traceback_or_private_fields(monkeypatch, capsys):
    import callstack.cli as cli

    class FailingStore:
        def __init__(self, db_path=None):
            pass

        async def initialize(self, **kwargs):
            raise RuntimeError("failed reading +155****0001 secret body")

        async def close(self):
            pass

    monkeypatch.setattr(cli, "SMSStore", FailingStore)

    code = cli.main(["inbox", "--sms-db-path", "/tmp/sms.sqlite", "--json"])

    captured = capsys.readouterr()
    assert code == 1
    assert "Error:" in captured.err
    assert "RuntimeError" in captured.err
    assert "Traceback" not in captured.err
    assert "+155****0001" not in captured.err
    assert "secret body" not in captured.err
