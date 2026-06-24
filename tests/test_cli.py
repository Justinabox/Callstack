"""Tests for the installable Callstack CLI."""

import json

from callstack.config import ModemConfig
from callstack.errors import SMSSendError
from callstack.network import RegistrationInfo, SignalInfo
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

def test_send_defaults_to_warning_logging_to_avoid_private_info_logs(monkeypatch, capsys):
    cli = _install_fake_modem(monkeypatch)

    code = cli.main(["send", "--to", "+155****4567", "--body", "secret passcode"])

    assert code == 0
    assert FakeModem.instances[0].config.log_level == "WARNING"
    capsys.readouterr()
