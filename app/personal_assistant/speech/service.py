"""Speech service used by the desktop UI and the voice status tool."""

from __future__ import annotations

import logging
import threading
from typing import Any

from ..config import Config
from .native_audio import AudioError, audio_status, record_audio
from .template_asr import TemplateASR, Transcription
from .tts import SpeechOutput
from .vad import quality_summary, trim_silence
from .vosk_asr import VoskASR
from .whisper_asr import WhisperASR

logger = logging.getLogger(__name__)


class SpeechService:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.sample_rate = int(config.get("speech.sample_rate", 16000))
        self.input_device = config.get("speech.input_device")
        self.output_device = config.get("speech.output_device")
        self._record_stop: threading.Event | None = None
        self._record_lock = threading.Lock()
        engine = str(config.get("speech.asr_engine", "template"))
        self.asr: TemplateASR | VoskASR | WhisperASR | None = None
        if engine == "template":
            self.asr = TemplateASR(
                config.path("speech.templates_dir"),
                sample_rate=self.sample_rate,
                threshold=float(config.get("speech.template_threshold", 0.62)),
                margin=float(config.get("speech.template_margin", 0.04)),
                vad_threshold=float(config.get("speech.vad_energy_threshold", 0.008)),
            )
        elif engine == "vosk":
            self.asr = VoskASR(
                config.path("speech.vosk_model_dir"),
                sample_rate=self.sample_rate,
                language=str(config.get("speech.vosk_language", "en-us")),
            )
        elif engine == "whisper":
            self.asr = WhisperASR(
                config.path("speech.whisper_model_dir"),
                sample_rate=self.sample_rate,
                language=str(config.get("speech.whisper_language", "en")),
                compute_type=str(config.get("speech.whisper_compute_type", "int8")),
                cpu_threads=int(config.get("speech.whisper_cpu_threads", 4)),
                beam_size=int(config.get("speech.whisper_beam_size", 5)),
                initial_prompt=str(config.get("speech.whisper_initial_prompt") or ""),
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
                "implemented": True,
                "word": str(self.config.get("speech.wake_word", "hearty")),
                "aliases": self.config.get("speech.wake_word_aliases", []),
                "reason": (
                    "Opt-in local wake-word listening. The microphone stays open only while "
                    "the listener is enabled; no audio leaves this PC."
                ),
            },
            "limitations": [
                "Template ASR recognizes only phrases recorded by the user; Vosk and Whisper recognize general English locally.",
                "Wake-word listening is opt-in and keeps the microphone open while it is active.",
                "Vosk uses a pretrained Kaldi model stored locally; Whisper uses a local CTranslate2 model. Neither calls a cloud service.",
                "No microphone recording happens without push-to-talk or an explicitly enabled wake-word listener.",
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
            transcription.reason = f"{transcription.reason} {quality_summary(vad)}".strip()
        return transcription

    def speak(self, text: str, stop_event: threading.Event | None = None) -> None:
        self.tts.speak(text, stop_event=stop_event)

    def stop_speaking(self) -> None:
        self.tts.stop()

    def stop_all(self) -> None:
        self.stop_recording()
        self.stop_speaking()
        if self.asr is not None and hasattr(self.asr, "close"):
            try:
                self.asr.close()
            except Exception:
                logger.debug("Could not stop the local speech recognizer", exc_info=True)
