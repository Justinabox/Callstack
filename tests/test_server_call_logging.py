"""Runtime tests for run_server call-handling log privacy."""

import logging

import pytest

import server


class _FakeCallSession:
    def __init__(self, number: str) -> None:
        self.number = number
        self.played: list[str] = []
        self.hung_up = False

    async def play(self, audio: str) -> None:
        self.played.append(audio)

    async def hangup(self) -> None:
        self.hung_up = True


class _FakeSMS:
    def on_message(self, callback) -> None:  # noqa: ANN001
        self._callback = callback


class _FakeBus:
    def subscribe(self, event_type, callback) -> None:  # noqa: ANN001
        pass


class _FakeModem:
    """Minimal Modem stand-in that drives the registered on_call handler once."""

    last_session: _FakeCallSession | None = None
    caller_number = ""

    def __init__(self, config) -> None:  # noqa: ANN001
        self._call_handler = None
        self.sms = _FakeSMS()
        self.bus = _FakeBus()

    async def __aenter__(self) -> "_FakeModem":
        return self

    async def __aexit__(self, *exc) -> bool:  # noqa: ANN002
        return False

    def on_call(self, fn):  # noqa: ANN001
        self._call_handler = fn
        return fn

    async def run_forever(self) -> None:
        session = _FakeCallSession(type(self).caller_number)
        type(self).last_session = session
        assert self._call_handler is not None
        await self._call_handler(session)


class _FakeRunner:
    def __init__(self, app) -> None:  # noqa: ANN001
        pass

    async def setup(self) -> None:
        pass

    async def cleanup(self) -> None:
        pass


class _FakeSite:
    def __init__(self, runner, host, port) -> None:  # noqa: ANN001
        pass

    async def start(self) -> None:
        pass


@pytest.mark.asyncio
async def test_incoming_call_log_masks_raw_caller_id(monkeypatch, caplog):
    raw_number = "+" + "1555" + "123" + "4567"
    monkeypatch.setattr(_FakeModem, "caller_number", raw_number)
    monkeypatch.setattr(_FakeModem, "last_session", None)

    monkeypatch.setattr(server, "Modem", _FakeModem)
    monkeypatch.setattr(server, "create_app", lambda modem, api_keys=None: object())
    monkeypatch.setattr(server.web, "AppRunner", _FakeRunner)
    monkeypatch.setattr(server.web, "TCPSite", _FakeSite)

    caplog.set_level(logging.INFO, logger="server")

    await server.run_server(object(), host="127.0.0.1", port=0)

    # The handler actually ran against a synthetic session.
    assert _FakeModem.last_session is not None
    assert _FakeModem.last_session.played == [server.AUDIO_GREET]
    assert _FakeModem.last_session.hung_up is True

    messages = [record.getMessage() for record in caplog.records]
    combined = "\n".join(messages)

    # Raw caller ID must never reach the logs.
    assert raw_number not in combined
    assert "1555" not in combined
    assert "123" not in combined
    # A masked representation must remain for operator correlation.
    assert "+***4567" in combined
    # The operational greeting signal must be preserved.
    assert "playing greeting" in combined
