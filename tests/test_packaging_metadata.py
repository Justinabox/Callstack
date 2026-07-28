"""Packaging metadata guards for documented install modes."""

from pathlib import Path
import json
import re
import tomllib


ROOT = Path(__file__).resolve().parents[1]

URL_PATTERN = re.compile(r"https?://[^\s`\"')]+")


def _fenced_code_block_urls(markdown):
    """URLs inside fenced code blocks - the values an operator copies verbatim."""

    blocks = re.findall(r"^```[^\n]*\n(.*?)^```", markdown, re.DOTALL | re.MULTILINE)
    return [url for block in blocks for url in URL_PATTERN.findall(block)]


def test_http_server_extra_declares_aiohttp_runtime_dependency():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())

    extras = pyproject["project"]["optional-dependencies"]

    assert "server" in extras
    assert any(dependency.startswith("aiohttp") for dependency in extras["server"])
    assert not any(
        dependency.startswith("pytest-aiohttp") for dependency in extras["server"]
    )


def test_dev_extra_declares_aiohttp_test_plugin_dependency():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())

    dev_dependencies = pyproject["project"]["optional-dependencies"]["dev"]

    assert any(dependency.startswith("pytest-aiohttp") for dependency in dev_dependencies)


def test_readme_documents_server_extra_for_http_mode():
    readme = (ROOT / "README.md").read_text()

    assert 'pip install -e ".[server,sqlite]"' in readme
    assert "callstack serve" in readme
    assert "python server.py" in readme


def test_readme_documents_packaged_serve_as_shipped_operator_flow():
    readme = (ROOT / "README.md").read_text()

    assert "packaged `callstack serve` is planned" not in readme
    assert "`callstack serve` is available" in readme
    assert "--api-key-file /etc/callstack/api-keys" in readme
    assert "install -d -m 700 /etc/callstack" in readme
    assert "install -d -m 700 /var/lib/callstack" in readme
    assert "CALLSTACK_SMS_DB_PATH=/var/lib/callstack/sms.sqlite3" in readme
    assert "loopback-only unauthenticated" in readme
    assert "CALLSTACK_BEARER_HEADER" in readme
    assert 'Authorization: Bearer ' in readme
    assert 'curl -fsS -H "$CALLSTACK_BEARER_HEADER" http://127.0.0.1:8080/healthz' in readme
    assert 'curl -fsS -H "$CALLSTACK_BEARER_HEADER" http://127.0.0.1:8080/metrics' in readme


def test_roadmap_lists_packaged_serve_as_shipped_not_pending():
    roadmap = (ROOT / "ROADMAP.md").read_text()

    assert "packaged `callstack serve`, active modem scan" not in roadmap
    assert "next CLI DX is packaged serve" not in roadmap
    assert "packaged `callstack serve` for HTTP server mode" in roadmap


def test_readme_documents_authenticated_pii_safe_websocket_endpoint():
    readme = (ROOT / "README.md").read_text()

    assert "`GET /ws`" in readme
    assert "authenticated WebSocket realtime feed" in readme
    assert "PII-safe typed events" in readme
    assert '"type": "hello"' in readme
    assert '"events"' in readme
    assert '"body": "[redacted]"' in readme
    assert "raw AT" in readme
    assert "not raw AT/modem traffic" in readme


def test_readme_websocket_hello_example_matches_supported_event_names():
    from server import SUPPORTED_WEBSOCKET_EVENTS

    readme = (ROOT / "README.md").read_text()
    hello_line = next(
        line for line in readme.splitlines() if line.startswith('{"type": "hello"')
    )

    assert json.loads(hello_line) == {
        "type": "hello",
        "version": 1,
        "events": list(SUPPORTED_WEBSOCKET_EVENTS),
        "selected_events": list(SUPPORTED_WEBSOCKET_EVENTS),
    }


def test_readme_websocket_filter_example_uses_supported_event_names():
    from server import SUPPORTED_WEBSOCKET_EVENTS

    readme = (ROOT / "README.md").read_text()

    assert "GET /ws?events=sms.received,sms.delivery_report" in readme
    assert "selected_events" in readme
    assert "raw.at" not in readme
    assert {"sms.received", "sms.delivery_report"}.issubset(SUPPORTED_WEBSOCKET_EVENTS)


def test_roadmap_lists_websocket_feed_as_shipped_not_planned():
    roadmap = (ROOT / "ROADMAP.md").read_text()

    assert "WebSocket Real-Time Feed" in roadmap
    assert "authenticated WebSocket realtime feed" in roadmap
    assert "WebSocket Feed | Medium | High | Planned" not in roadmap
    assert "planned after SMS/security foundations" not in roadmap
    assert "durable replay" in roadmap
    assert "dashboard" in roadmap
    assert "replay/filtering" not in roadmap


def test_deployment_guide_documents_hardened_systemd_service_unit():
    guide = (ROOT / "docs" / "deployment.md").read_text()

    assert 'pip install -e ".[server,sqlite]"' in guide
    assert "install -d -m 700 /etc/callstack" in guide
    assert "install -d -m 700 /var/lib/callstack" in guide
    assert "install -m 600 /dev/null /etc/callstack/api-keys" in guide
    assert "EnvironmentFile=/etc/callstack/callstack.env" in guide
    assert (
        "ExecStart=/usr/local/bin/callstack serve --host 127.0.0.1 --port 8080 "
        "--api-key-file /etc/callstack/api-keys" in guide
    )
    for directive in (
        "NoNewPrivileges=yes",
        "PrivateTmp=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "ReadWritePaths=/var/lib/callstack",
    ):
        assert directive in guide


def test_deployment_guide_tells_operators_to_resolve_the_executable_path():
    guide = " ".join((ROOT / "docs" / "deployment.md").read_text().split())

    assert "command -v callstack" in guide
    assert (
        "Replace the `ExecStart=` binary path with that output" in guide
    )
    assert "`/usr/local/bin/callstack` is only an example" in guide


def test_deployment_guide_warns_that_non_loopback_needs_api_keys():
    guide = (ROOT / "docs" / "deployment.md").read_text()

    assert "--host 0.0.0.0" in guide
    assert (
        "Non-loopback binds require API keys or an equivalent trusted network boundary"
        in guide
    )
    assert "--allow-unauthenticated-loopback" in guide
    assert "loopback-only unauthenticated" in guide


def test_deployment_guide_smoke_checks_use_unechoed_bearer_variable():
    guide = (ROOT / "docs" / "deployment.md").read_text()

    assert (
        'CALLSTACK_BEARER_HEADER="$(awk \'NF {print "Authorization: Bearer " $0; exit}\' '
        "/etc/callstack/api-keys)\"" in guide
    )
    assert 'curl -fsS -H "$CALLSTACK_BEARER_HEADER" http://127.0.0.1:8080/healthz' in guide
    assert 'curl -fsS -H "$CALLSTACK_BEARER_HEADER" http://127.0.0.1:8080/metrics' in guide
    assert 'echo "$CALLSTACK_BEARER_HEADER"' not in guide
    assert 'cat /etc/callstack/api-keys' not in guide
    assert not re.search(r'-H\s+"Authorization: Bearer', guide)


def test_deployment_guide_smoke_checks_carry_no_raw_api_key_material():
    guide = (ROOT / "docs" / "deployment.md").read_text()

    assert (
        'CALLSTACK_BEARER_HEADER="$(awk \'NF {print "Authorization: Bearer " $0; exit}\' '
        "/etc/callstack/api-keys)\"" in guide
    )
    assert not re.search(r"\b[A-Z][A-Z0-9_]*API_KEY[A-Z0-9_]*=(?!\"?\$)\S", guide)
    assert not re.search(r"Authorization: Bearer (?![$\"'])\S", guide)


def test_deployment_guide_contains_no_private_identifiers():
    guide = (ROOT / "docs" / "deployment.md").read_text()

    assert not re.search(r"\+\d{6,}", guide)
    assert not re.search(r"\bAT\+C(PIN|IMI|CID)=?\s*\d", guide)


def test_fenced_code_block_url_scan_ignores_prose_documentation_links():
    markdown = "\n".join(
        [
            "See the [systemd manual](https://www.freedesktop.org/software/systemd/man/systemd.exec.html).",
            "",
            "```bash",
            "curl -X POST https://hooks.example.net/inbound-sms",
            "```",
            "",
        ]
    )

    assert _fenced_code_block_urls(markdown) == ["https://hooks.example.net/inbound-sms"]


def test_deployment_guide_carries_no_example_webhook_target_url():
    guide = (ROOT / "docs" / "deployment.md").read_text()
    webhook_section = guide.split("## 7. Webhooks and log hygiene")[1].split("\n## ")[0]

    assert "contains no example webhook target" in " ".join(webhook_section.split())
    assert URL_PATTERN.findall(webhook_section) == []
    assert [
        url
        for url in _fenced_code_block_urls(guide)
        if not url.startswith("http://127.0.0.1:8080/")
    ] == []


def test_readme_links_production_deployment_guide_from_http_server_mode():
    readme = (ROOT / "README.md").read_text()

    assert "docs/deployment.md" in readme
    assert "production Raspberry Pi deployment guide" in readme


def test_roadmap_lists_systemd_and_scrape_guidance_as_shipped():
    roadmap = (ROOT / "ROADMAP.md").read_text()

    assert "systemd-style deployment examples, and production-safe health/metrics" not in roadmap
    assert "docs/deployment.md" in roadmap
    assert "production-safe health/metrics scrape guidance" in roadmap
    assert "conservative audio-port assignment" in roadmap


def test_packaged_console_script_includes_server_helper_module():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())

    setuptools_config = pyproject["tool"]["setuptools"]

    assert "server" in setuptools_config["py-modules"]
