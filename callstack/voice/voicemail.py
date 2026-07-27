"""Local voicemail capture: greeting -> record -> optional goodbye -> hangup."""

import json
import math
import os
import secrets
import tempfile
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class VoicemailMessage:
    """Immutable metadata for a captured voicemail message."""

    id: str
    audio_path: str
    started_at: datetime
    ended_at: datetime
    duration_seconds: float
    byte_size: int
    caller: str
    termination_reason: str


class VoicemailBox:
    """Captures voicemail locally using CallSession-shaped record/play APIs."""

    def __init__(
        self,
        directory,
        greeting,
        max_duration: float = 120.0,
        stop_on_dtmf: bool = True,
        goodbye=None,
    ):
        self._directory = self._validate_pathlike(directory, "directory")
        self._greeting = self._validate_pathlike(greeting, "greeting")
        self._goodbye = (
            None if goodbye is None else self._validate_pathlike(goodbye, "goodbye")
        )
        if (
            type(max_duration) is bool
            or not isinstance(max_duration, (int, float))
            or not math.isfinite(max_duration)
            or max_duration <= 0
        ):
            raise ValueError("max_duration must be a positive finite number")
        self._max_duration = float(max_duration)
        self._stop_on_dtmf = stop_on_dtmf

    @staticmethod
    def _validate_pathlike(value, name: str) -> Path:
        if value is None or isinstance(value, bool):
            raise ValueError(f"{name} must be a non-empty path")
        try:
            text = os.fspath(value)
        except TypeError as exc:
            raise ValueError(f"{name} must be a non-empty path") from exc
        if not str(text).strip():
            raise ValueError(f"{name} must be a non-empty path")
        return Path(text)

    async def record(self, session) -> VoicemailMessage:
        """Capture one voicemail: greeting, record, optional goodbye, hangup."""
        if not session.is_active:
            raise RuntimeError("Cannot capture voicemail without an active call")

        self._ensure_directory()

        try:
            await session.play(str(self._greeting))

            started_at = datetime.now(timezone.utc)
            output_path = self._directory / self._generate_filename(started_at)
            await session.record(
                str(output_path),
                max_duration=self._max_duration,
                stop_on_dtmf=self._stop_on_dtmf,
            )
            ended_at = datetime.now(timezone.utc)

            if self._goodbye is not None and session.is_active:
                await session.play(str(self._goodbye))
        except Exception:
            if session.is_active:
                try:
                    await session.hangup()
                except Exception:
                    pass
            raise

        if session.is_active:
            try:
                await session.hangup()
            except RuntimeError:
                pass

        byte_size, duration_seconds = self._inspect_wav(output_path)

        message = VoicemailMessage(
            id=output_path.stem,
            audio_path=str(output_path),
            started_at=started_at,
            ended_at=ended_at,
            duration_seconds=duration_seconds,
            byte_size=byte_size,
            caller=session.number,
            termination_reason="completed",
        )
        self._write_sidecar(output_path, message)
        return message

    def _ensure_directory(self) -> None:
        try:
            self._directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        except FileExistsError:
            pass
        else:
            self._directory.chmod(0o700)

    @staticmethod
    def _generate_filename(timestamp: datetime) -> str:
        stamp = timestamp.strftime("%Y%m%dT%H%M%S%f")
        token = secrets.token_hex(4)
        return f"voicemail_{stamp}Z_{token}.wav"

    @staticmethod
    def _inspect_wav(path: Path) -> tuple[int, float]:
        byte_size = path.stat().st_size
        with wave.open(str(path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
        duration_seconds = frames / rate if rate else 0.0
        return byte_size, duration_seconds

    @staticmethod
    def _write_sidecar(audio_path: Path, message: VoicemailMessage) -> None:
        sidecar_path = audio_path.with_suffix(".json")
        payload = {
            "id": message.id,
            "audio_path": message.audio_path,
            "started_at": message.started_at.isoformat(),
            "ended_at": message.ended_at.isoformat(),
            "duration_seconds": message.duration_seconds,
            "byte_size": message.byte_size,
            "caller": message.caller,
            "termination_reason": message.termination_reason,
        }
        fd, tmp_name = tempfile.mkstemp(
            dir=audio_path.parent, prefix=f"{sidecar_path.name}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, sidecar_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
