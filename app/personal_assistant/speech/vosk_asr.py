"""Optional offline general-purpose speech recognition using Vosk.

Provenance: Vosk is an Apache-2.0 offline speech-recognition toolkit. The acoustic
model is a pretrained Kaldi model downloaded once by the owner and stored under
``models/vosk``. It runs entirely on this PC: no audio, transcript, or feature is
sent anywhere, and there is no automatic cloud fallback.

This engine is opt-in. The original from-scratch template matcher remains
available as ``asr_engine: template``.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

import numpy as np

from .template_asr import Transcription
from .voice_text import normalize_spoken_text

logger = logging.getLogger(__name__)

DEFAULT_MODEL_NAME = "vosk-model-small-en-us-0.15"

# A Vosk model is read-only and can serve several recognizers. The wake listener
# and push-to-talk service would otherwise each load their own ~437 MB copy,
# which fails on Windows systems with a small commit limit. Share one model per
# directory for the lifetime of the process.
_MODELS: dict[str, Any] = {}
_MODELS_LOCK = threading.Lock()


def _shared_model(model_dir: Path) -> Any:
    key = str(model_dir.resolve()).lower()
    with _MODELS_LOCK:
        model = _MODELS.get(key)
        if model is None:
            from vosk import Model, SetLogLevel

            SetLogLevel(-1)
            model = Model(str(model_dir))
            _MODELS[key] = model
        return model


class VoskASR:
    engine = "vosk-offline"

    def __init__(
        self,
        model_dir: Path,
        *,
        sample_rate: int = 16000,
        language: str = "en-us",
    ) -> None:
        self.model_dir = Path(model_dir)
        self.sample_rate = int(sample_rate)
        self.language = str(language or "en-us")
        self._model: Any = None
        self._load_error = ""
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self.model_dir.exists():
            self._load_error = (
                f"Vosk model not found at {self.model_dir}. "
                "Run scripts/install_voice_model.py to download it once."
            )
            return
        if not (self.model_dir / "am").exists() or not (self.model_dir / "conf").exists():
            self._load_error = (
                f"{self.model_dir} does not look like a Vosk model (missing am/ or conf/)."
            )
            return
        try:
            self._model = _shared_model(self.model_dir)
        except ImportError:
            self._load_error = "The 'vosk' package is not installed. Run pip install vosk."
        except Exception as exc:
            self._load_error = f"Vosk model could not be loaded: {exc.__class__.__name__}: {exc}"
            logger.exception("Vosk model load failed")

    @property
    def available(self) -> bool:
        return self._model is not None

    @property
    def reason(self) -> str:
        return self._load_error

    def status(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "engine": self.engine,
            "model_dir": str(self.model_dir),
            "model_name": self.model_dir.name,
            "language": self.language,
            "sample_rate": self.sample_rate,
            "reason": self._load_error,
            "policy": (
                "Optional local pretrained Vosk/Kaldi model, Apache-2.0. "
                "Runs offline after the one-time model download; no cloud fallback. "
                "The model is shared by local recognizers in this process."
            ),
        }

    def create_recognizer(self, grammar: list[str] | None = None) -> Any:
        """Create a streaming recognizer owned by one capture thread.

        ``grammar`` restricts decoding to a small set of words or phrases. Vosk
        compiles that list into a much smaller search graph, which is both far
        faster and more accurate than open-vocabulary decoding on a noisy
        microphone. An empty or ``None`` grammar keeps full dictation mode.
        """
        if self._model is None:
            raise RuntimeError(self._load_error or "Vosk is unavailable.")
        from vosk import KaldiRecognizer

        phrases = [str(item).strip() for item in (grammar or []) if str(item).strip()]
        if phrases:
            recognizer = KaldiRecognizer(self._model, self.sample_rate, json.dumps(phrases))
        else:
            recognizer = KaldiRecognizer(self._model, self.sample_rate)
        recognizer.SetWords(True)
        return recognizer

    def recognize(self, samples: np.ndarray | bytes, sample_rate: int | None = None) -> Transcription:
        if self._model is None:
            return Transcription(
                text="",
                confidence=0.0,
                engine=self.engine,
                available=False,
                reason=self._load_error or "Vosk is unavailable.",
            )
        rate = int(sample_rate or self.sample_rate)
        if isinstance(samples, (bytes, bytearray)):
            raw = bytes(samples)
            raw = raw[: len(raw) - (len(raw) % 2)]
            values = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        else:
            values = np.asarray(samples, dtype=np.float32)
        if rate != self.sample_rate:
            from .native_audio import resample

            values = resample(values, rate, self.sample_rate)
        pcm = (np.clip(values, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        if not pcm:
            return Transcription("", 0.0, engine=self.engine, available=True, reason="No audio was captured.")
        try:
            with self._lock:
                recognizer = self.create_recognizer()
            recognizer.AcceptWaveform(pcm)
            payload = json.loads(recognizer.FinalResult())
        except Exception as exc:
            logger.exception("Vosk recognition failed")
            return Transcription(
                "",
                0.0,
                engine=self.engine,
                available=False,
                reason=f"Vosk recognition failed: {exc.__class__.__name__}: {exc}",
            )
        text = str(payload.get("text") or "").replace("[unk]", " ").strip()
        words = payload.get("result") or []
        confidences = [float(item.get("conf", 0.0)) for item in words if isinstance(item, dict)]
        confidence = sum(confidences) / len(confidences) if confidences else (1.0 if text else 0.0)
        if not text:
            return Transcription(
                "",
                confidence,
                engine=self.engine,
                available=True,
                reason="No speech was recognized. Try again closer to the microphone.",
            )
        return Transcription(
            text,
            confidence,
            engine=self.engine,
            available=True,
            reason=f"Recognized by the local Vosk model ({self.model_dir.name}).",
        )
