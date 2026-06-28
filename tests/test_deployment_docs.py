"""Regression tests for production deployment documentation."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT_GUIDE = ROOT / "docs" / "deployment.md"
DOC_PATHS = [ROOT / "README.md", ROOT / "ROADMAP.md", DEPLOYMENT_GUIDE]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_deployment_guide_documents_systemd_and_authenticated_smoke_checks():
    guide = _read(DEPLOYMENT_GUIDE)

    assert "# Production deployment" in guide
    assert "pip install -e \".[server,sqlite]\"" in guide
    assert "getent group callstack >/dev/null 2>&1 || groupadd --system callstack" in guide
    assert "useradd --system --gid callstack --home /var/lib/callstack --shell /usr/sbin/nologin callstack" in guide
    assert "install -d -m 750 -o root -g callstack /etc/callstack" in guide
    assert "install -d -m 700 -o callstack -g callstack /var/lib/callstack" in guide
    assert "install -m 640 -o root -g callstack /dev/null /etc/callstack/api-keys" in guide
    assert "install -m 640 -o root -g callstack /dev/null /etc/callstack/callstack.env" in guide
    assert "CALLSTACK_SMS_DB_PATH=/var/lib/callstack/sms.sqlite3" in guide
    assert "EnvironmentFile=/etc/callstack/callstack.env" in guide
    assert "ExecStart=/opt/callstack/venv/bin/callstack serve --host 127.0.0.1 --port 8080 --api-key-file /etc/callstack/api-keys" in guide
    assert "NoNewPrivileges=true" in guide
    assert "ProtectSystem=full" in guide
    assert "ProtectHome=true" in guide
    assert "curl -fsS -H \"$CALLSTACK_BEARER_HEADER\" http://127.0.0.1:8080/healthz" in guide
    assert "curl -fsS -H \"$CALLSTACK_BEARER_HEADER\" http://127.0.0.1:8080/metrics" in guide
    assert "callstack_uptime_seconds" in guide
    assert "non-loopback" in guide
    assert "API keys" in guide
    assert "Prometheus" in guide
    assert "Authorization: Bearer ***" not in guide


def test_readme_and_roadmap_link_production_deployment_guide():
    readme = _read(ROOT / "README.md")
    roadmap = _read(ROOT / "ROADMAP.md")

    assert "docs/deployment.md" in readme
    assert "production deployment guide" in readme.lower()
    assert "getent group callstack >/dev/null 2>&1 || groupadd --system callstack" in readme
    assert "useradd --system --gid callstack --home /var/lib/callstack --shell /usr/sbin/nologin callstack" in readme
    assert "install -d -m 750 -o root -g callstack /etc/callstack" in readme
    assert "install -d -m 700 -o callstack -g callstack /var/lib/callstack" in readme
    assert "install -d -m 700 /etc/callstack" not in readme
    assert "production deployment guide" in roadmap.lower()
    assert "systemd-style deployment examples" not in roadmap
    assert "production-safe health/metrics scrape guidance" not in roadmap


def test_documentation_examples_do_not_include_private_identifiers_or_invalid_bearer_redaction():
    phone_like = re.compile(r"(?<![#\w])\+?\d[\d -]{9,}\d(?![\w])")
    modem_identifier = re.compile(r"\b(?:IMEI|IMSI|ICCID|MEID|ESN)\s*[:=]\s*[A-Za-z0-9-]{6,}", re.IGNORECASE)
    webhook_example = re.compile(r"https?://[^\s)]+webhook|webhook[^\s)]*https?://", re.IGNORECASE)

    for path in DOC_PATHS:
        text = _read(path)
        assert not phone_like.search(text), f"{path.name} contains a 10+ digit phone-like value"
        assert not modem_identifier.search(text), f"{path.name} contains a modem/SIM identifier example"
        assert not webhook_example.search(text), f"{path.name} contains a raw webhook URL example"
        assert "Authorization: Bearer ***" not in text, f"{path.name} documents an invalid redacted bearer header"
        assert "my-secret-key" not in text, f"{path.name} contains a secret-looking API key example"


def test_deployment_guide_bash_snippets_are_syntax_valid():
    guide = _read(DEPLOYMENT_GUIDE)
    snippets = re.findall(r"```bash\n(.*?)\n```", guide, flags=re.DOTALL)

    assert snippets, "deployment guide should include shell snippets"
    for snippet in snippets:
        subprocess.run(["bash", "-n"], input=snippet, text=True, check=True)
