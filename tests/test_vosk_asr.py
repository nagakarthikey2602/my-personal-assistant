"""Smoke tests for the optional local Vosk recogniser.

The recognition test is skipped unless the owner has installed the model with
``scripts/install_voice_model.py``. No microphone is opened and no audio is
played; the silence check synthesises PCM in memory only.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from personal_assistant.speech.vosk_asr import VoskASR

MODEL_DIR = (
    Path(__file__).resolve().parents[1]
    / "models"
    / "vosk"
    / "vosk-model-small-en-us-0.15"
)


def test_missing_model_is_reported_without_importing_vosk(workspace: Path) -> None:
    asr = VoskASR(workspace / "missing-vosk-model")

    assert asr.available is False
    assert "install_voice_model.py" in asr.reason
    status = asr.status()
    assert status["available"] is False
    assert status["engine"] == "vosk-offline"
    assert status["model_dir"].endswith("missing-vosk-model")


def test_missing_model_recognition_is_unavailable(workspace: Path) -> None:
    asr = VoskASR(workspace / "missing-vosk-model")
    result = asr.recognize(np.zeros(1600, dtype=np.float32), 16000)

    assert result.available is False
    assert result.text == ""
    assert "install_voice_model.py" in result.reason


@pytest.mark.skipif(not MODEL_DIR.exists(), reason="The local Vosk model is not installed.")
def test_installed_model_is_shared_between_instances() -> None:
    pytest.importorskip("vosk")

    first = VoskASR(MODEL_DIR)
    second = VoskASR(MODEL_DIR)

    assert first.available, first.reason
    assert second.available, second.reason
    assert first._model is second._model


@pytest.mark.skipif(not MODEL_DIR.exists(), reason="The local Vosk model is not installed.")
def test_installed_model_loads_and_handles_silence() -> None:
    pytest.importorskip("vosk")

    asr = VoskASR(MODEL_DIR)
    assert asr.available, asr.reason
    assert asr.status()["model_name"] == MODEL_DIR.name

    recognizer = asr.create_recognizer()
    assert recognizer is not None

    result = asr.recognize(np.zeros(16000, dtype=np.float32), 16000)
    assert result.available is True
    assert result.text == ""
    assert result.engine == "vosk-offline"
