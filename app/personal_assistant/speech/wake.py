"""Opt-in wake-word listener for the local assistant.

Wake detection uses a small grammar-constrained Vosk recognizer. Restricting
Vosk to the wake word and a short list of self-contained commands makes decoding
much faster and more accurate than open-vocabulary dictation, especially when
there is background speech. Once the wake word is heard, a general command is
transcribed by the local Whisper model if it is installed, or by the constrained
Vosk result when the phrase is already complete.

The microphone stream is open only while listening is explicitly enabled. No
audio is written to disk or uploaded. When Whisper is configured, raw PCM crosses
a local pipe to a persistent child process; nothing leaves this PC.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import deque
from typing import Any, Callable

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import numpy as np

from ..config import Config
from .native_audio import AudioError, MicrophoneStream, audio_status
from .voice_text import clean_command, find_wake_command, normalize_spoken_text, wake_aliases
from .vosk_asr import VoskASR

logger = logging.getLogger(__name__)

StatusCallback = Callable[[str, str], None]
CommandCallback = Callable[[str, float], None]

_GREETINGS = ("hey", "hi", "ok", "okay")
_CAPTURE_SILENCE_SECONDS = 0.8
_CAPTURE_FRAME_MS = 50

# Complete, self-contained commands that the constrained recognizer can decode
# without knowing any arguments. Anything else is sent to Whisper after the
# wake word. Keep this list focused: every extra phrase makes the wake-word
# search graph larger and can dilute the wake-word path.
DEFAULT_COMMAND_PHRASES: tuple[str, ...] = (
    "status",
    "help",
    "capabilities",
    "undo",
    "what do you remember",
    "take a screenshot",
    "screenshot",
    "open notepad",
    "open calculator",
    "open chrome",
    "open edge",
    "list sandbox",
    "list my windows",
    "battery status",
    "what is my battery status",
    "wifi status",
    "bluetooth status",
    "volume up",
    "volume down",
    "play",
    "pause",
    "next track",
    "previous track",
    "what time is it",
    "what is the date",
    "lock the screen",
    "kill switch",
    "resume",
    "stop",
    "cancel",
    "diagnose",
)


def build_wake_grammar(
    wake_word: str,
    aliases: list[str] | tuple[str, ...] | None = None,
    extra_phrases: list[str] | None = None,
) -> list[str]:
    """Build the constrained Vosk vocabulary used while waiting for the wake word."""
    words = wake_aliases(wake_word, aliases)
    commands = list(DEFAULT_COMMAND_PHRASES)
    for item in extra_phrases or []:
        phrase = re.sub(r"[^a-z0-9 ]+", " ", str(item).lower())
        phrase = re.sub(r"\s+", " ", phrase).strip()
        if phrase and phrase not in commands:
            commands.append(phrase)
    phrases: list[str] = []
    for alias in words:
        phrases.append(alias)
        for greeting in _GREETINGS:
            phrases.append(f"{greeting} {alias}")
    for alias in words:
        for command in commands:
            phrases.append(f"{alias} {command}")
    phrases.append("[unk]")
    unique: list[str] = []
    for phrase in phrases:
        if phrase not in unique:
            unique.append(phrase)
    return unique


def _pcm_samples(data: bytes) -> np.ndarray:
    usable = len(data) - (len(data) % 2)
    if usable <= 0:
        return np.array([], dtype=np.float32)
    return np.frombuffer(data[:usable], dtype="<i2").astype(np.float32) / 32768.0


def _pcm_level(data: bytes) -> float:
    samples = _pcm_samples(data)
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples * samples) + 1e-12))


def _trailing_silence_seconds(data: bytes, sample_rate: int, threshold: float) -> float:
    """Measure the silent run at the end of a PCM block."""
    samples = _pcm_samples(data)
    frame = max(1, int(sample_rate * _CAPTURE_FRAME_MS / 1000))
    count = samples.size // frame
    silent = 0
    for index in range(count - 1, -1, -1):
        block = samples[index * frame:(index + 1) * frame]
        if float(np.sqrt(np.mean(block * block) + 1e-12)) >= threshold:
            break
        silent += frame
    return silent / float(sample_rate)


class _AudioBuffer:
    """A small rolling PCM buffer used to replay audio spoken before wake detection."""

    def __init__(self, sample_rate: int, max_seconds: float) -> None:
        self.sample_rate = int(sample_rate)
        self.max_bytes = max(1, int(max_seconds * self.sample_rate) * 2)
        self._chunks: deque[tuple[int, bytes]] = deque()
        self._end = 0

    @property
    def end_seconds(self) -> float:
        return self._end / (self.sample_rate * 2)

    def append(self, data: bytes) -> None:
        if not data:
            return
        self._chunks.append((self._end, bytes(data)))
        self._end += len(data)
        while self._chunks:
            start, _chunk = self._chunks[0]
            if self._end - start <= self.max_bytes:
                break
            self._chunks.popleft()

    def since(self, seconds: float) -> bytes:
        offset = max(0, int(seconds * self.sample_rate) * 2)
        if not self._chunks:
            return b""
        offset = max(offset, self._chunks[0][0])
        parts = bytearray()
        for start, chunk in self._chunks:
            end = start + len(chunk)
            if end <= offset:
                continue
            parts.extend(chunk[max(0, offset - start):])
        return bytes(parts)

    def clear(self) -> None:
        self._chunks.clear()
        self._end = 0


class WakeWordListener:
    def __init__(
        self,
        config: Config,
        *,
        on_status: StatusCallback | None = None,
        on_command: CommandCallback | None = None,
        stream_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self.on_status = on_status
        self.on_command = on_command
        self._stream_factory = stream_factory
        self.sample_rate = int(config.get("speech.sample_rate", 16000))
        self.wake_word = str(config.get("speech.wake_word", "hearty"))
        configured = config.get("speech.wake_word_aliases") or []
        if not isinstance(configured, list):
            configured = []
        self.aliases = wake_aliases(self.wake_word, configured)
        self.asr = VoskASR(
            config.path("speech.vosk_model_dir"),
            sample_rate=self.sample_rate,
            language=str(config.get("speech.vosk_language", "en-us")),
        )
        extra_phrases = config.get("speech.wake_command_phrases") or []
        if not isinstance(extra_phrases, list):
            extra_phrases = []
        self.wake_grammar = build_wake_grammar(self.wake_word, self.aliases, extra_phrases)
        self.command_engine = str(config.get("speech.wake_command_engine", "whisper")).lower()
        whisper_dir = config.get("speech.whisper_model_dir")
        self.command_model_dir = config.path("speech.whisper_model_dir") if whisper_dir else None
        self.command_seconds = max(1.0, float(config.get("speech.wake_command_seconds", 6.0)))
        self._command_asr: Any = None
        self._command_error = ""
        self._stop = threading.Event()
        self._muted = threading.Event()
        self._thread: threading.Thread | None = None
        self._stream: MicrophoneStream | None = None
        self._lock = threading.Lock()
        self._state = "stopped"
        self._detail = "Wake-word listening is off."
        self._last_error = ""
        self._base_seconds = 0.0
        self._fed_seconds = 0.0
        self._audio = _AudioBuffer(self.sample_rate, max(12.0, self.command_seconds + 5.0))

    # Lifecycle -----------------------------------------------------------
    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive() and not self._stop.is_set())

    @property
    def state(self) -> str:
        return self._state

    @property
    def detail(self) -> str:
        return self._detail

    def start(self) -> tuple[bool, str]:
        if self.running:
            return True, self._detail
        if not self.asr.available:
            message = self.asr.reason or "The Vosk speech model is unavailable."
            self._set_state("error", message)
            return False, message
        audio = audio_status()
        if self._stream_factory is None and not audio.get("input_available"):
            message = "No microphone input is available."
            self._set_state("error", message)
            return False, message
        self._stop.clear()
        self._muted.clear()
        self._last_error = ""
        self._thread = threading.Thread(target=self._run, name="wake-word-listener", daemon=True)
        self._thread.start()
        return True, f"Starting wake-word listening for '{self.wake_word}'."

    def stop(self) -> None:
        self._stop.set()
        stream = self._stream
        if stream is not None:
            try:
                stream.close()
            except Exception:
                logger.debug("Microphone stream close failed during shutdown", exc_info=True)
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3.5)
        self._thread = None
        self._set_state("stopped", "Wake-word listening is off.")

    def mute(self, reason: str = "Wake-word listener paused while an action runs.") -> None:
        if not self.running:
            return
        self._muted.set()
        self._set_state("muted", reason)

    def resume(self) -> None:
        if not self.running:
            return
        self._muted.clear()
        self._set_state("listening", f"Listening for '{self.wake_word}'.")

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "state": self._state,
            "detail": self._detail,
            "wake_word": self.wake_word,
            "aliases": list(self.aliases),
            "model": self.asr.status(),
            "wake_grammar_phrases": len(self.wake_grammar),
            "command_engine": {
                "engine": self.command_engine,
                "model_dir": str(self.command_model_dir) if self.command_model_dir else "",
                "available": self._command_asr.available if self._command_asr is not None else None,
                "reason": (
                    self._command_asr.reason
                    if self._command_asr is not None
                    else self._command_error
                ),
            },
            "last_error": self._last_error,
        }

    # Capture loop --------------------------------------------------------
    def _run(self) -> None:
        stream: MicrophoneStream | None = None
        recognizer: Any = None
        try:
            factory = self._stream_factory or MicrophoneStream
            stream = factory(
                sample_rate=self.sample_rate,
                device_index=self.config.get("speech.input_device"),
                chunk_ms=200,
                buffer_count=4,
            )
            with self._lock:
                self._stream = stream
            recognizer = self.asr.create_recognizer(grammar=self.wake_grammar)
            if self.command_engine == "whisper":
                # Importing and loading CTranslate2 takes a second or more on
                # first use. Do it before announcing readiness so the first
                # spoken command is not delayed by model startup.
                self._set_state("loading", "Loading the local command speech model…")
                self._command_asr_instance()
            self._base_seconds = 0.0
            self._fed_seconds = 0.0
            self._audio.clear()
            self._set_state("listening", f"Listening for '{self.wake_word}'.")
            wake_timeout = max(2.0, float(self.config.get("speech.wake_command_timeout", 10.0)))
            settle = max(0.2, float(self.config.get("speech.wake_command_settle_seconds", 0.65)))
            vad_threshold = max(0.001, float(self.config.get("speech.vad_energy_threshold", 0.008)))
            awake = False
            awake_deadline = 0.0
            pending = ""
            pending_confidence = 0.0
            command_deadline = 0.0
            capturing = False
            capture_deadline = 0.0
            capture_done = False
            capture_speech = False
            capture_silence = 0.0
            captured = bytearray()
            was_muted = False
            bytes_per_second = self.sample_rate * 2
            while not self._stop.is_set():
                data = stream.read(timeout=0.25)
                now = time.monotonic()
                if self._stop.is_set():
                    break
                if self._muted.is_set():
                    if not was_muted:
                        self._reset_recognizer(recognizer)
                        pending = ""
                        pending_confidence = 0.0
                        awake = False
                        capturing = False
                        capture_done = False
                        capture_speech = False
                        capture_silence = 0.0
                        captured = bytearray()
                        was_muted = True
                    continue
                if was_muted:
                    self._reset_recognizer(recognizer)
                    was_muted = False
                if pending and now >= command_deadline:
                    self._dispatch_command(pending, pending_confidence)
                    pending = ""
                    pending_confidence = 0.0
                    awake = False
                    self._reset_recognizer(recognizer)
                    continue
                if awake and not pending and not capturing and now >= awake_deadline:
                    awake = False
                    self._reset_recognizer(recognizer)
                    self._set_state("listening", f"Listening for '{self.wake_word}'.")
                if capturing and (capture_done or now >= capture_deadline):
                    text, confidence = self._transcribe_command(bytes(captured))
                    capturing = False
                    capture_done = False
                    capture_speech = False
                    capture_silence = 0.0
                    captured = bytearray()
                    awake = False
                    self._reset_recognizer(recognizer)
                    if text:
                        pending = text
                        pending_confidence = confidence
                        command_deadline = now + settle
                        self._set_state("awake", "Heard you. Finishing the command…")
                    else:
                        self._set_state("listening", f"Listening for '{self.wake_word}'.")
                    continue
                if not data:
                    # A WinMM read can time out briefly, and finite test streams
                    # end. Feed a short silent block so Vosk can finalize.
                    data = bytes(max(2, int(self.sample_rate * 0.2)) * 2)
                self._audio.append(data)
                self._fed_seconds += len(data) / bytes_per_second
                if capturing:
                    captured.extend(data)
                    if _pcm_level(data) >= vad_threshold:
                        capture_speech = True
                        capture_silence = 0.0
                    elif capture_speech:
                        capture_silence += len(data) / bytes_per_second
                        if capture_silence >= _CAPTURE_SILENCE_SECONDS:
                            capture_done = True
                    continue
                if not recognizer.AcceptWaveform(data):
                    continue
                payload = json.loads(recognizer.Result())
                text = normalize_spoken_text(payload.get("text") or "")
                if not text:
                    continue
                heard, command = find_wake_command(text, self.wake_word, self.aliases)
                if not heard:
                    continue
                confidence = _confidence(payload, text)
                self._acknowledge()
                awake = True
                awake_deadline = now + wake_timeout
                if command:
                    pending = clean_command(command)
                    pending_confidence = confidence
                    command_deadline = now + settle
                    self._set_state("awake", "Wake word heard. Capturing your command.")
                    continue
                wake_end = self._wake_end_seconds(payload)
                captured = bytearray(self._audio.since(max(0.0, wake_end + 0.05)))
                capture_done = False
                capture_speech = _pcm_level(bytes(captured)) >= vad_threshold
                capture_silence = _trailing_silence_seconds(
                    bytes(captured), self.sample_rate, vad_threshold
                )
                if capture_speech and capture_silence >= _CAPTURE_SILENCE_SECONDS:
                    capture_done = True
                capturing = True
                capture_deadline = now + self.command_seconds
                self._set_state("awake", "Wake word heard. Listening for your command.")
        except AudioError as exc:
            self._last_error = str(exc)
            self._set_state("error", f"Wake-word microphone error: {exc}")
        except Exception as exc:
            self._last_error = f"{exc.__class__.__name__}: {exc}"
            logger.exception("Wake-word listener failed")
            self._set_state("error", f"Wake-word listener error: {self._last_error}")
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            with self._lock:
                self._stream = None
            self._close_command_asr()
            self._set_state("stopped", "Wake-word listening stopped.")

    def _reset_recognizer(self, recognizer: Any) -> None:
        """Restart Vosk and the rolling buffer on one zero-based clock."""
        recognizer.Reset()
        self._base_seconds = 0.0
        self._fed_seconds = 0.0
        self._audio.clear()

    def _wake_end_seconds(self, payload: dict[str, Any]) -> float:
        tokens = {token for alias in self.aliases for token in alias.split()}
        for item in payload.get("result") or []:
            if not isinstance(item, dict):
                continue
            word = str(item.get("word") or "").strip().lower()
            if word in tokens:
                try:
                    return self._base_seconds + float(item.get("end", 0.0))
                except (TypeError, ValueError):
                    break
        return max(0.0, self._fed_seconds - 0.25)

    def _command_asr_instance(self) -> Any:
        if self._command_asr is not None or self._command_error:
            return self._command_asr
        if self.command_engine != "whisper":
            return None
        if self.command_model_dir is None:
            self._command_error = "speech.whisper_model_dir is not configured."
            return None
        from .whisper_asr import WhisperASR

        prompt = str(self.config.get("speech.whisper_initial_prompt") or "").strip()
        if not prompt:
            prompt = (
                f"{self.wake_word} status. {self.wake_word} open notepad. "
                f"{self.wake_word} take a screenshot."
            )
        self._command_asr = WhisperASR(
            self.command_model_dir,
            sample_rate=self.sample_rate,
            language=str(self.config.get("speech.whisper_language", "en")),
            compute_type=str(self.config.get("speech.whisper_compute_type", "int8")),
            cpu_threads=int(self.config.get("speech.whisper_cpu_threads", 4)),
            beam_size=int(self.config.get("speech.whisper_beam_size", 5)),
            initial_prompt=prompt,
        )
        if not self._command_asr.available:
            self._command_error = self._command_asr.reason
            self._command_asr = None
        elif not self._command_asr.preload():
            self._command_error = self._command_asr.reason
            self._command_asr = None
        return self._command_asr

    def _close_command_asr(self) -> None:
        engine = self._command_asr
        self._command_asr = None
        self._command_error = ""
        if engine is not None and hasattr(engine, "close"):
            try:
                engine.close()
            except Exception:
                logger.debug("Could not stop the command speech worker", exc_info=True)

    def _transcribe_command(self, pcm: bytes) -> tuple[str, float]:
        engine = self._command_asr_instance()
        if engine is not None:
            result = engine.recognize(pcm, self.sample_rate)
            if result.available:
                text = clean_command(_first_sentence(result.text))
                if text:
                    heard, command = find_wake_command(text, self.wake_word, self.aliases)
                    if heard:
                        text = clean_command(command)
                return text, float(result.confidence)
            # The worker died or timed out after startup. Drop it and let the
            # local Vosk fallback handle this command rather than losing it.
            reason = result.reason or "The local Whisper worker is unavailable."
            logger.warning("Whisper command engine failed mid-session: %s", reason)
            self._close_command_asr()
            self._command_error = reason
        # Last-resort path: the dedicated command engine is unavailable, so use
        # the Vosk open-vocabulary recognizer. It is slower but remains local and
        # still gives the parser a chance instead of silently dropping the command.
        if not self.asr.available:
            return "", 0.0
        result = self.asr.recognize(pcm, self.sample_rate)
        text = clean_command(_first_sentence(result.text))
        return text, float(result.confidence)

    def _dispatch_command(self, text: str, confidence: float) -> None:
        text = clean_command(text)
        if not text:
            return
        self._muted.set()
        self._set_state("muted", f"Command captured: {text}")
        callback = self.on_command
        if callback is None:
            return
        try:
            callback(text, float(confidence))
        except Exception:
            logger.exception("Wake-word command callback failed")

    def _acknowledge(self) -> None:
        if not bool(self.config.get("speech.wake_ack_sound", True)):
            return

        def beep() -> None:
            try:
                import winsound

                winsound.Beep(880, 120)
            except Exception:
                pass

        threading.Thread(target=beep, name="wake-word-beep", daemon=True).start()

    def _set_state(self, state: str, detail: str) -> None:
        self._state = state
        self._detail = detail
        callback = self.on_status
        if callback is None:
            return
        try:
            callback(state, detail)
        except Exception:
            logger.exception("Wake-word status callback failed")


def _first_sentence(text: str) -> str:
    value = str(text or "").strip()
    parts = re.split(r"(?<=[.!?])\s+", value, maxsplit=1)
    return parts[0].strip() if parts else value


def _confidence(payload: dict[str, Any], text: str) -> float:
    words = payload.get("result") or []
    values = [float(item.get("conf", 0.0)) for item in words if isinstance(item, dict)]
    return sum(values) / len(values) if values else (1.0 if text else 0.0)
