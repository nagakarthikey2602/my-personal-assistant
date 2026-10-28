"""System status and voice subsystem checks that never touch user media.

No microphone is opened, no audio is played, and media keys are simulated only
inside this process by replacing the Win32 call with a recorder.
"""

from __future__ import annotations

import os

import pytest

if os.name != "nt":
    pytest.skip("These checks use the Windows system and audio APIs.", allow_module_level=True)

pytestmark = pytest.mark.windows


def test_system_status_is_read_only_and_verified(tool) -> None:
    call, _assistant = tool

    info = call("system.info")
    assert info.status == "ok", info.message
    assert info.data["offline_mode"] == "strict"
    assert info.data["logical_cpus"] >= 1
    assert info.data["memory_total"] > 0
    assert "Python sockets" in info.data["network_guard"]["scope"]

    top = call("system.top_processes", sort="memory", limit=3)
    assert top.status == "ok", top.message
    assert len(top.data["processes"]) <= 3
    assert top.data["total_processes"] >= 1
    assert all(item["memory_bytes"] >= 0 for item in top.data["processes"])

    battery = call("system.battery")
    assert battery.status == "ok", battery.message
    if "percent" not in battery.data:
        assert "No battery" in battery.message
    else:
        assert 0 <= battery.data["percent"] <= 100
        assert isinstance(battery.data["plugged_in"], bool)

    offline = call("system.offline_status")
    assert offline.status == "ok", offline.message
    assert offline.data["mode"] == "strict"
    assert offline.data["browser_network_enabled"] is False
    assert "child processes" in offline.data["note"]


def test_audio_device_enumeration(tool) -> None:
    call, _assistant = tool
    audio = call("system.audio_devices")
    assert audio.status == "ok", audio.message
    devices = audio.data["devices"]
    assert isinstance(devices["inputs"], list)
    assert isinstance(devices["outputs"], list)


def test_media_key_is_simulated_and_reported_unverified(tool, monkeypatch) -> None:
    call, _assistant = tool
    from personal_assistant.tools import system

    sent: list[tuple] = []
    monkeypatch.setattr(system.user32, "keybd_event", lambda *args: sent.append(args))
    result = call("system.media_control", action="mute")
    assert result.status == "unverified"
    assert result.verified is False
    assert result.data["verification"] == "unavailable"
    assert len(sent) == 2  # key down and key up


def test_voice_status_capabilities_and_disabled_speech(tool) -> None:
    call, _assistant = tool

    status = call("voice.status")
    assert status.status == "ok", status.message
    assert status.data["tts"]["configured_engine"] == "none"
    assert status.data["tts"]["available"] is False
    assert status.data["asr"]["engine"] == "none"
    assert status.data["wake_word"]["implemented"] is True

    devices = call("voice.list_devices")
    assert devices.status == "ok", devices.message
    assert "inputs" in devices.data and "outputs" in devices.data

    capabilities = call("voice.capabilities")
    assert capabilities.status == "ok"
    assert "cloud STT/TTS" in capabilities.data["not_implemented"]
    assert "speaker diarization" in capabilities.data["not_implemented"]
    assert "user-recorded MFCC/DTW phrase recognition" in capabilities.data["implemented"]
    assert "optional local Vosk general English recognition" in capabilities.data["implemented"]
    assert "optional local faster-whisper command recognition" in capabilities.data["implemented"]
    assert "opt-in local wake-word listening ('hearty')" in capabilities.data["implemented"]

    spoken = call("voice.speak", text="This must not reach a speaker.")
    assert spoken.status == "error"
    assert "disabled" in spoken.message.lower()


def test_formant_synthesizer_builds_speech_without_playback() -> None:
    from personal_assistant.speech.tts import FormantSynthesizer, text_to_phonemes

    tokens = text_to_phonemes("hello 123")
    assert "HH" in tokens
    assert "PAUSE_WORD" in tokens

    samples = FormantSynthesizer().synthesize("hello world")
    assert samples.size > 0
    assert samples.dtype.name == "float32"
    assert float(abs(samples).max()) <= 1.0
