"""Tests for VoicemailBox local voicemail capture."""

import json
import math
import stat
import wave
from pathlib import Path

import pytest

from callstack.voice.voicemail import VoicemailBox, VoicemailMessage


def _write_wav(path, rate=8000, channels=1, sampwidth=2, num_frames=1600):
    """Create a real test WAV file, standing in for a session.record() result."""
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(rate)
        wf.writeframes(b"\x00" * num_frames * channels * sampwidth)
    return path


class FakeCallSession:
    """Deterministic CallSession-shaped fake: no hardware, no mocks."""

    def __init__(self, number="+15551234567", wav_frames=1600, wav_rate=8000):
        self.number = number
        self._is_active = True
        self.calls = []
        self.record_kwargs = None
        self._wav_frames = wav_frames
        self._wav_rate = wav_rate
        self.disconnect_after_record = False
        self.record_raises = None
        self.hangup_raises_concurrent_disconnect = False

    @property
    def is_active(self):
        return self._is_active

    async def play(self, audio_path):
        self.calls.append(("play", audio_path))

    async def record(self, output_path, max_duration=60.0, stop_on_dtmf=False):
        self.calls.append(("record", output_path))
        self.record_kwargs = {"max_duration": max_duration, "stop_on_dtmf": stop_on_dtmf}
        if self.record_raises is not None:
            raise self.record_raises
        _write_wav(output_path, rate=self._wav_rate, num_frames=self._wav_frames)
        if self.disconnect_after_record:
            self._is_active = False
        return output_path

    async def hangup(self):
        self.calls.append(("hangup",))
        self._is_active = False
        if self.hangup_raises_concurrent_disconnect:
            raise RuntimeError("Cannot hang up without an active call")


async def test_record_happy_path_end_to_end(tmp_path):
    directory = tmp_path / "voicemail"
    greeting = "greeting.wav"
    goodbye = "goodbye.wav"
    box = VoicemailBox(
        directory=directory, greeting=greeting, goodbye=goodbye, max_duration=30.0
    )
    session = FakeCallSession(number="+15551234567")

    assert not directory.exists()

    message = await box.record(session)

    assert isinstance(message, VoicemailMessage)
    assert directory.exists()

    # Ordering: greeting -> record -> goodbye -> hangup
    assert session.calls[0] == ("play", greeting)
    assert session.calls[1][0] == "record"
    assert session.calls[2] == ("play", goodbye)
    assert session.calls[3] == ("hangup",)

    # session.record() kwargs
    assert session.record_kwargs == {"max_duration": 30.0, "stop_on_dtmf": True}

    audio_path = Path(message.audio_path)
    assert audio_path.exists()
    assert audio_path.parent == directory
    assert audio_path.suffix == ".wav"

    # Privacy-safe filename: never contains the caller number
    assert "5551234567" not in audio_path.name
    assert "+15551234567" not in audio_path.name

    sidecar_path = audio_path.with_suffix(".json")
    assert sidecar_path.exists()
    data = json.loads(sidecar_path.read_text())

    assert data["id"] == message.id
    assert data["audio_path"] == message.audio_path
    assert data["caller"] == "+15551234567"
    assert data["termination_reason"] == "completed" == message.termination_reason
    assert data["byte_size"] == message.byte_size == audio_path.stat().st_size
    assert data["duration_seconds"] == pytest.approx(message.duration_seconds)
    assert message.duration_seconds == pytest.approx(1600 / 8000)
    assert "started_at" in data
    assert "ended_at" in data


async def test_record_skips_goodbye_and_hangup_when_already_disconnected(tmp_path):
    box = VoicemailBox(
        directory=tmp_path / "voicemail", greeting="greeting.wav", goodbye="goodbye.wav"
    )
    session = FakeCallSession()
    session.disconnect_after_record = True

    message = await box.record(session)

    assert ("hangup",) not in session.calls
    assert ("play", "goodbye.wav") not in session.calls
    assert message.termination_reason == "completed"


async def test_record_rejects_already_inactive_session_before_any_action(tmp_path):
    directory = tmp_path / "voicemail"
    box = VoicemailBox(
        directory=directory, greeting="greeting.wav", goodbye="goodbye.wav"
    )
    session = FakeCallSession()
    session._is_active = False

    with pytest.raises(RuntimeError):
        await box.record(session)

    assert session.calls == []
    assert not directory.exists()


async def test_record_failure_hangs_up_once_and_writes_no_sidecar(tmp_path):
    directory = tmp_path / "voicemail"
    box = VoicemailBox(
        directory=directory, greeting="greeting.wav", goodbye="goodbye.wav"
    )
    session = FakeCallSession()
    session.record_raises = RuntimeError("hardware failure")

    with pytest.raises(RuntimeError, match="hardware failure"):
        await box.record(session)

    assert session.calls.count(("hangup",)) == 1
    assert list(directory.glob("*.json")) == []


async def test_record_creates_directory_and_sidecar_with_restrictive_permissions(tmp_path):
    directory = tmp_path / "voicemail"
    box = VoicemailBox(directory=directory, greeting="greeting.wav")
    session = FakeCallSession()

    message = await box.record(session)

    dir_mode = stat.S_IMODE(directory.stat().st_mode)
    assert dir_mode == 0o700

    sidecar_path = Path(message.audio_path).with_suffix(".json")
    sidecar_mode = stat.S_IMODE(sidecar_path.stat().st_mode)
    assert sidecar_mode == 0o600


async def test_record_tolerates_concurrent_disconnect_during_hangup_on_success(tmp_path):
    directory = tmp_path / "voicemail"
    box = VoicemailBox(
        directory=directory, greeting="greeting.wav", goodbye="goodbye.wav"
    )
    session = FakeCallSession()
    session.hangup_raises_concurrent_disconnect = True

    message = await box.record(session)

    assert isinstance(message, VoicemailMessage)
    sidecar_path = Path(message.audio_path).with_suffix(".json")
    assert sidecar_path.exists()


async def test_sidecar_write_failure_leaves_no_json_but_keeps_wav(tmp_path, monkeypatch):
    from callstack.voice import voicemail as voicemail_module

    directory = tmp_path / "voicemail"
    box = VoicemailBox(directory=directory, greeting="greeting.wav")
    session = FakeCallSession()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(voicemail_module.json, "dump", boom)

    with pytest.raises(OSError, match="disk full"):
        await box.record(session)

    files = sorted(p.name for p in directory.iterdir())
    assert len(files) == 1
    assert files[0].endswith(".wav")


@pytest.mark.parametrize("bad_duration", [0, -1.0, math.inf, math.nan, True, "30"])
def test_init_rejects_invalid_max_duration(bad_duration, tmp_path):
    with pytest.raises(ValueError):
        VoicemailBox(
            directory=tmp_path / "voicemail",
            greeting="greeting.wav",
            max_duration=bad_duration,
        )


def test_voicemail_symbols_are_exported_from_public_apis():
    import callstack
    import callstack.voice
    from callstack.voice import voicemail as voicemail_module

    assert callstack.VoicemailBox is voicemail_module.VoicemailBox
    assert callstack.VoicemailMessage is voicemail_module.VoicemailMessage
    assert callstack.voice.VoicemailBox is voicemail_module.VoicemailBox
    assert callstack.voice.VoicemailMessage is voicemail_module.VoicemailMessage
