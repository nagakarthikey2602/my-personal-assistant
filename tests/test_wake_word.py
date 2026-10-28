"""Wake-word listener lifecycle checks that never open a microphone."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from personal_assistant.config import Config
from personal_assistant.speech import wake as wake_module
from personal_assistant.speech.template_asr import Transcription
from personal_assistant.speech.voice_text import find_wake_command
from personal_assistant.speech.wake import WakeWordListener, build_wake_grammar


class _NeverUsedStream:
    def __init__(self, **_kwargs: object) -> None:
        raise AssertionError("The stream must not open when the model is missing.")

    def read(self, timeout: float = 0.5) -> bytes:  # pragma: no cover - defensive
        return b""

    def close(self) -> None:
        pass


def _wake_config(workspace: Path) -> Config:
    return Config.from_dict(
        {
            "paths": {"write_roots": [str(workspace / "sandbox")]},
            "speech": {
                "asr_engine": "vosk",
                "tts_engine": "none",
                "wake_word_enabled": True,
                "wake_word": "hearty",
                "wake_command_engine": "vosk",
                "vosk_model_dir": str(workspace / "missing-vosk-model"),
            },
        },
        workspace,
    )


def test_wake_listener_reports_a_missing_model_without_starting(workspace: Path) -> None:
    events: list[tuple[str, str]] = []
    listener = WakeWordListener(
        _wake_config(workspace),
        on_status=lambda state, detail: events.append((state, detail)),
        stream_factory=_NeverUsedStream,
    )

    ok, message = listener.start()

    assert ok is False
    assert "install_voice_model.py" in message
    assert listener.running is False
    assert events and events[-1][0] == "error"
    assert listener.status()["aliases"]
    assert listener.status()["wake_word"] == "hearty"


def test_wake_listener_status_defaults_to_stopped(workspace: Path) -> None:
    listener = WakeWordListener(_wake_config(workspace), stream_factory=_NeverUsedStream)
    status = listener.status()

    assert status["running"] is False
    assert status["state"] == "stopped"
    assert status["detail"] == "Wake-word listening is off."
    assert status["model"]["available"] is False


class _FakeRecognizer:
    def __init__(self) -> None:
        self.emitted = False

    def AcceptWaveform(self, _data: bytes) -> bool:
        if self.emitted:
            return False
        self.emitted = True
        return True

    def Result(self) -> str:
        return json.dumps(
            {
                "text": "hardy open notepad",
                "result": [{"word": "hardy", "conf": 0.9}, {"word": "open", "conf": 0.9}, {"word": "notepad", "conf": 0.9}],
            }
        )

    def Reset(self) -> None:
        pass


class _FakeASR:
    available = True
    reason = ""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.recognizer = _FakeRecognizer()
        self.grammar: list[str] | None = None

    def create_recognizer(self, grammar: list[str] | None = None) -> _FakeRecognizer:
        self.grammar = list(grammar or [])
        return self.recognizer

    def status(self) -> dict[str, object]:
        return {"available": True, "engine": "vosk-offline", "reason": ""}


class _ScriptedStream:
    def __init__(self, **_kwargs: object) -> None:
        self.closed = False
        self.sent = False

    def read(self, timeout: float = 0.25) -> bytes:
        if not self.sent:
            self.sent = True
            return b"\x00\x00" * 1600
        return b""

    def close(self) -> None:
        self.closed = True


def test_wake_listener_dispatches_a_command_from_a_fake_stream(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wake_module, "VoskASR", _FakeASR)
    events: list[tuple[str, str]] = []
    commands: list[tuple[str, float]] = []
    streams: list[_ScriptedStream] = []

    def factory(**_kwargs: object) -> _ScriptedStream:
        stream = _ScriptedStream()
        streams.append(stream)
        return stream

    listener = WakeWordListener(
        _wake_config(workspace),
        on_status=lambda state, detail: events.append((state, detail)),
        on_command=lambda text, confidence: commands.append((text, confidence)),
        stream_factory=factory,
    )

    ok, message = listener.start()
    assert ok, message

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not commands:
        time.sleep(0.02)

    assert commands == [("open notepad", 0.9)]
    assert any(state == "awake" for state, _detail in events)
    assert listener.state == "muted"
    assert "hearty open notepad" in listener.asr.grammar

    listener.stop()

    assert listener.state == "stopped"
    assert streams and streams[0].closed


def test_stopping_before_the_command_deadline_does_not_dispatch(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wake_module, "VoskASR", _FakeASR)
    events: list[tuple[str, str]] = []
    commands: list[tuple[str, float]] = []
    config = _wake_config(workspace)
    config.set("speech.wake_command_settle_seconds", 3.0)

    listener = WakeWordListener(
        config,
        on_status=lambda state, detail: events.append((state, detail)),
        on_command=lambda text, confidence: commands.append((text, confidence)),
        stream_factory=_ScriptedStream,
    )

    ok, message = listener.start()
    assert ok, message

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not any(state == "awake" for state, _detail in events):
        time.sleep(0.02)
    assert any(state == "awake" for state, _detail in events)

    listener.stop()
    time.sleep(0.5)

    assert commands == []
    assert listener.state == "stopped"


def test_failed_whisper_worker_falls_back_to_vosk(workspace: Path) -> None:
    config = _wake_config(workspace)
    config.set("speech.wake_command_engine", "whisper")
    listener = WakeWordListener(config, stream_factory=_NeverUsedStream)

    class _DeadWhisper:
        available = True

        def __init__(self) -> None:
            self.closed = False

        def recognize(self, _pcm: bytes, _rate: int) -> Transcription:
            return Transcription("", 0.0, engine="whisper-offline", available=False, reason="worker died")

        def close(self) -> None:
            self.closed = True

    class _VoskFallback:
        available = True
        reason = ""

        def recognize(self, _pcm: bytes, _rate: int) -> Transcription:
            return Transcription("open notepad", 0.9, engine="vosk-offline")

    dead = _DeadWhisper()
    listener._command_asr = dead
    listener.asr = _VoskFallback()

    text, confidence = listener._transcribe_command(b"\x00\x00" * 1600)

    assert text == "open notepad"
    assert confidence == 0.9
    assert dead.closed is True
    assert listener._command_asr is None
    assert listener._command_error == "worker died"


def test_wake_grammar_contains_aliases_and_self_contained_commands() -> None:
    grammar = build_wake_grammar("hearty", ["hardy"], ["open calculator"])

    assert "hearty" in grammar
    assert "hardy status" in grammar
    assert "hearty open notepad" in grammar
    assert "hearty open calculator" in grammar
    assert "[unk]" in grammar


def test_find_wake_command_accepts_leading_unknown_tokens() -> None:
    heard, command = find_wake_command("[unk] [unk] hearty open notepad [unk]")

    assert heard is True
    assert command == "open notepad"


def test_find_wake_command_keeps_only_the_first_command() -> None:
    heard, command = find_wake_command("hearty status hearty open notepad")

    assert heard is True
    assert command == "status"


def test_find_wake_command_does_not_match_inside_words() -> None:
    heard, command = find_wake_command("the heartiest status")

    assert heard is False


def test_audio_buffer_replays_only_the_requested_window() -> None:
    buffer = wake_module._AudioBuffer(sample_rate=1000, max_seconds=5)
    buffer.append(b"a" * 2000)  # 1 second
    buffer.append(b"b" * 2000)  # 2 seconds
    buffer.append(b"c" * 2000)  # 3 seconds

    assert buffer.since(1.0) == b"b" * 2000 + b"c" * 2000
    assert buffer.since(2.5) == b"c" * 1000


def test_capture_silence_helper_measures_only_the_trailing_gap() -> None:
    import numpy as np

    rate = 16000
    tone = (np.sin(np.arange(rate // 2) * 0.1) * 0.2 * 32767).astype("<i2").tobytes()
    silence = b"\x00\x00" * (rate * 8 // 10)
    audio = silence[: rate] + tone + silence

    assert wake_module._pcm_level(silence) < 0.001
    assert wake_module._pcm_level(tone) > 0.1
    assert wake_module._trailing_silence_seconds(audio, rate, 0.008) == pytest.approx(0.8, abs=0.06)
