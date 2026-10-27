"""Speech service used by the desktop UI and the voice status tool."""

from __future__ import annotations

import threading
from typing import Any

from ..config import Config
from .native_audio import AudioError, audio_status, record_audio
from .template_asr import TemplateASR, Transcription
from .tts import SpeechOutput
from .vad import quality_summary, trim_silence


class SpeechService:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.sample_rate = int(config.get("speech.sample_rate", 16000))
        self.input_device = config.get("speech.input_device")
        self.output_device = config.get("speech.output_device")
        self._record_stop: threading.Event | None = None
        self._record_lock = threading.Lock()
        engine = str(config.get("speech.asr_engine", "template"))
        self.asr: TemplateASR | None = None
        if engine == "template":
            self.asr = TemplateASR(
                config.path("speech.templates_dir"),
                sample_rate=self.sample_rate,
                threshold=float(config.get("speech.template_threshold", 0.62)),
                margin=float(config.get("speech.template_margin", 0.04)),
                vad_threshold=float(config.get("speech.vad_energy_threshold", 0.008)),
            )
        self.tts = SpeechOutput(config)

    def status(self) -> dict[str, Any]:
        return {
            "asr": self.asr.status() if self.asr else {
                "available": False,
                "engine": "none",
                "reason": "ASR is disabled in configuration.",
            },
            "tts": self.tts.status(),
            "audio": audio_status(),
            "input_device": self.input_device,
            "output_device": self.output_device,
            "wake_word": {
                "enabled": bool(self.config.get("speech.wake_word_enabled")),
                "implemented": False,
                "reason": "Always-listening wake word is intentionally not implemented in this milestone.",
            },
            "limitations": [
                "Template ASR recognizes only phrases recorded by the user; it is not general dictation.",
                "No microphone recording happens without an explicit push-to-talk action.",
                "Formant TTS is robotic and has not been perceptually validated.",
                "OS pretrained voices are opt-in only and disabled by default under the model policy.",
            ],
        }

    @property
    def recording(self) -> bool:
        with self._record_lock:
            return self._record_stop is not None

    def stop_recording(self) -> None:
        with self._record_lock:
            if self._record_stop is not None:
                self._record_stop.set()

    def record_and_transcribe(self, max_seconds: float | None = None) -> Transcription:
        if self.asr is None:
            return Transcription("", 0.0, available=False, engine="none", reason="Speech recognition is disabled.")
        seconds = float(max_seconds or self.config.get("speech.push_to_talk_seconds", 8))
        seconds = min(max(seconds, 0.5), float(self.config.get("speech.max_speech_seconds", 15)))
        with self._record_lock:
            if self._record_stop is not None:
                return Transcription("", 0.0, available=True, reason="A recording is already in progress.")
            stop_event = threading.Event()
            self._record_stop = stop_event
        try:
            try:
                samples, rate = record_audio(seconds, self.sample_rate, self.input_device, stop_event)
            except AudioError as exc:
                return Transcription("", 0.0, available=False, reason=str(exc))
        finally:
            with self._record_lock:
                self._record_stop = None
        vad = trim_silence(samples, rate, float(self.config.get("speech.vad_energy_threshold", 0.008)))
        if not vad.has_speech:
            return Transcription("", 0.0, available=True, reason=vad.reason)
        transcription = self.asr.recognize(vad.trimmed, rate)
        if transcription.text:
            transcription.reason = quality_summary(vad)
        return transcription

    def speak(self, text: str, stop_event: threading.Event | None = None) -> None:
        self.tts.speak(text, stop_event=stop_event)

    def stop_speaking(self) -> None:
        self.tts.stop()

    def stop_all(self) -> None:
        self.stop_recording()
        self.stop_speaking()
