"""Packaging metadata guards for documented install modes."""

from pathlib import Path
import json
import tomllib


ROOT = Path(__file__).resolve().parents[1]


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
    assert 'curl -H "$CALLSTACK_BEARER_HEADER" http://127.0.0.1:8080/healthz' in readme
    assert 'curl -H "$CALLSTACK_BEARER_HEADER" http://127.0.0.1:8080/metrics' in readme


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
        "cursor": 0,
        "replay_window": 128,
    }


def test_roadmap_lists_websocket_feed_as_shipped_not_planned():
    roadmap = (ROOT / "ROADMAP.md").read_text()

    assert "WebSocket Real-Time Feed" in roadmap
    assert "authenticated WebSocket realtime feed" in roadmap
    assert "WebSocket Feed | Medium | High | Planned" not in roadmap
    assert "planned after SMS/security foundations" not in roadmap
    assert "durable replay" in roadmap
    assert "dashboard" in roadmap


def test_packaged_console_script_includes_server_helper_module():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())

    setuptools_config = pyproject["tool"]["setuptools"]

    assert "server" in setuptools_config["py-modules"]
