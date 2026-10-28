"""Optional offline general-purpose speech recognition using faster-whisper.

Provenance: faster-whisper (MIT) runs OpenAI's Whisper model (MIT) through
CTranslate2 on the local CPU. The model is downloaded once by the owner into
``models/whisper`` and is loaded from that directory only. No audio, transcript,
or feature is sent anywhere, and there is no automatic cloud fallback.

Whisper runs in a persistent local child process. Vosk (used for wake detection)
and CTranslate2 each bundle an Intel OpenMP/MKL runtime; loading both in one
process makes model startup fail on some Windows systems with
``mkl_malloc: failed to allocate memory``. The worker keeps the two runtimes
isolated while still loading the model only once. Audio crosses a local pipe as
raw PCM; it is never written to disk or sent over a network.
"""

from __future__ import annotations

import importlib.util
import logging
import multiprocessing as mp
import os
import threading
from pathlib import Path
from typing import Any

# Keep numeric libraries to one worker each. This is both faster for a tiny
# model and avoids OpenBLAS/MKL thread-pool allocations failing on Windows
# systems with no page file and a nearly full commit limit.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import numpy as np

from .template_asr import Transcription
from .voice_text import normalize_spoken_text

logger = logging.getLogger(__name__)

DEFAULT_MODEL_NAME = "faster-whisper-tiny.en"
REQUIRED_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")
STARTUP_TIMEOUT = 60.0
REQUEST_TIMEOUT = 120.0


def _worker_main(connection: Any, options: dict[str, Any]) -> None:
    """Load one Whisper model and serve transcription requests over a pipe."""
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    # CTranslate2's MKL int8 kernels reserve a large workspace. With Vosk loaded
    # in the parent process and no page file, that allocation can fail with
    # "mkl_malloc: failed to allocate memory". Its bundled packed-GEMM path is
    # slightly slower but reliable and still fully local. Force these values
    # rather than trusting the caller's environment.
    os.environ["CT2_USE_MKL"] = "0"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    try:
        from faster_whisper import WhisperModel

        model = WhisperModel(
            options["model_dir"],
            device="cpu",
            compute_type=options["compute_type"],
            cpu_threads=options["cpu_threads"],
            local_files_only=True,
        )
    except Exception as exc:
        try:
            connection.send(
                {
                    "ready": False,
                    "reason": f"Whisper model could not be loaded: {exc.__class__.__name__}: {exc}",
                }
            )
        except Exception:
            pass
        return
    try:
        connection.send({"ready": True})
    except Exception:
        return

    prompt = str(options.get("initial_prompt") or "").strip() or None
    while True:
        try:
            pcm = connection.recv_bytes()
        except (EOFError, OSError):
            break
        if not pcm:
            break
        try:
            audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
            segments, _info = model.transcribe(
                audio,
                language=options["language"],
                beam_size=options["beam_size"],
                vad_filter=False,
                condition_on_previous_text=False,
                initial_prompt=prompt,
                without_timestamps=True,
            )
            collected = list(segments)
            text = normalize_spoken_text(
                " ".join(str(segment.text or "") for segment in collected)
            )
            logprobs = [
                float(segment.avg_logprob)
                for segment in collected
                if getattr(segment, "avg_logprob", None) is not None
            ]
            confidence = float(np.exp(np.mean(logprobs))) if logprobs else (1.0 if text else 0.0)
            confidence = max(0.0, min(1.0, confidence))
            reason = "" if text else "No speech was recognized. Try again closer to the microphone."
            connection.send(
                {"text": text, "confidence": confidence, "reason": reason}
            )
        except Exception as exc:
            try:
                connection.send(
                    {
                        "text": "",
                        "confidence": 0.0,
                        "reason": f"Whisper recognition failed: {exc.__class__.__name__}: {exc}",
                    }
                )
            except Exception:
                break
    try:
        connection.close()
    except Exception:
        pass


class WhisperASR:
    engine = "whisper-offline"

    def __init__(
        self,
        model_dir: Path,
        *,
        sample_rate: int = 16000,
        language: str = "en",
        compute_type: str = "int8",
        cpu_threads: int = 4,
        beam_size: int = 5,
        initial_prompt: str = "",
    ) -> None:
        self.model_dir = Path(model_dir)
        self.sample_rate = int(sample_rate)
        self.language = str(language or "en")
        self.compute_type = str(compute_type or "int8")
        self.cpu_threads = max(1, int(cpu_threads))
        self.beam_size = max(1, int(beam_size))
        self.initial_prompt = str(initial_prompt or "")
        self._load_error = ""
        self._state_lock = threading.RLock()
        self._io_lock = threading.Lock()
        self._context = mp.get_context("spawn")
        self._process: Any = None
        self._connection: Any = None
        self._validate()

    def _validate(self) -> None:
        if not self.model_dir.exists():
            self._load_error = (
                f"Whisper model not found at {self.model_dir}. "
                "Run scripts/install_whisper_model.py to download it once."
            )
            return
        missing = [name for name in REQUIRED_FILES if not (self.model_dir / name).is_file()]
        if missing:
            self._load_error = (
                f"{self.model_dir} does not look like a faster-whisper model "
                f"(missing {', '.join(missing)})."
            )
            return
        if importlib.util.find_spec("faster_whisper") is None:
            self._load_error = (
                "The 'faster-whisper' package is not installed. Run pip install faster-whisper."
            )

    @property
    def available(self) -> bool:
        return not self._load_error

    @property
    def reason(self) -> str:
        return self._load_error

    def preload(self) -> bool:
        """Start the local worker and load the model if it is not already ready."""
        with self._state_lock:
            if self._load_error:
                return False
            if self._process is not None and self._process.is_alive():
                return True
            self._close_locked()
            parent, child = self._context.Pipe()
            process = self._context.Process(
                target=_worker_main,
                args=(
                    child,
                    {
                        "model_dir": str(self.model_dir),
                        "language": self.language,
                        "compute_type": self.compute_type,
                        "cpu_threads": self.cpu_threads,
                        "beam_size": self.beam_size,
                        "initial_prompt": self.initial_prompt,
                    },
                ),
                name="whisper-command-worker",
                daemon=True,
            )
            try:
                process.start()
            except Exception as exc:
                parent.close()
                child.close()
                self._load_error = (
                    f"Whisper worker could not start: {exc.__class__.__name__}: {exc}"
                )
                return False
            child.close()
            self._process = process
            self._connection = parent
            if not parent.poll(STARTUP_TIMEOUT):
                self._load_error = "Whisper worker did not become ready in time."
                self._close_locked()
                return False
            try:
                message = parent.recv()
            except (EOFError, OSError) as exc:
                self._load_error = f"Whisper worker stopped during startup: {exc.__class__.__name__}"
                self._close_locked()
                return False
            if not message.get("ready"):
                self._load_error = str(message.get("reason") or "Whisper model could not be loaded.")
                self._close_locked()
                return False
            return True

    def status(self) -> dict[str, Any]:
        process = self._process
        return {
            "available": self.available,
            "engine": self.engine,
            "model_dir": str(self.model_dir),
            "model_name": self.model_dir.name,
            "language": self.language,
            "sample_rate": self.sample_rate,
            "compute_type": self.compute_type,
            "worker_running": bool(process is not None and process.is_alive()),
            "reason": self._load_error,
            "policy": (
                "Optional local CTranslate2/Whisper model, MIT-licensed. "
                "Loaded from disk in a local worker process; no cloud fallback or runtime download."
            ),
        }

    def recognize(self, samples: np.ndarray | bytes, sample_rate: int | None = None) -> Transcription:
        if self._load_error:
            return Transcription(
                text="",
                confidence=0.0,
                engine=self.engine,
                available=False,
                reason=self._load_error,
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
        if values.size == 0:
            return Transcription("", 0.0, engine=self.engine, available=True, reason="No audio was captured.")
        values = np.clip(values, -1.0, 1.0)
        if not self.preload():
            return Transcription(
                text="",
                confidence=0.0,
                engine=self.engine,
                available=False,
                reason=self._load_error or "Whisper is unavailable.",
            )
        pcm = (values * 32767).astype("<i2").tobytes()
        with self._io_lock:
            process = self._process
            connection = self._connection
            if process is None or connection is None or not process.is_alive():
                self._load_error = "Whisper worker is not running."
                return Transcription("", 0.0, engine=self.engine, available=False, reason=self._load_error)
            try:
                connection.send_bytes(pcm)
                if not connection.poll(REQUEST_TIMEOUT):
                    self._load_error = "Whisper worker did not answer in time."
                    return Transcription("", 0.0, engine=self.engine, available=False, reason=self._load_error)
                response = connection.recv()
            except (EOFError, OSError) as exc:
                self._load_error = f"Whisper worker stopped: {exc.__class__.__name__}"
                return Transcription("", 0.0, engine=self.engine, available=False, reason=self._load_error)
        text = normalize_spoken_text(str(response.get("text") or ""))
        confidence = max(0.0, min(1.0, float(response.get("confidence") or 0.0)))
        reason = str(response.get("reason") or "")
        if not text:
            return Transcription(
                "",
                confidence,
                engine=self.engine,
                available=True,
                reason=reason or "No speech was recognized. Try again closer to the microphone.",
            )
        return Transcription(
            text,
            confidence,
            engine=self.engine,
            available=True,
            reason=reason or f"Recognized by the local Whisper model ({self.model_dir.name}).",
        )

    def close(self) -> None:
        with self._state_lock:
            self._close_locked()

    def _close_locked(self) -> None:
        connection = self._connection
        process = self._process
        self._connection = None
        self._process = None
        if connection is not None:
            try:
                connection.send_bytes(b"")
            except Exception:
                pass
            try:
                connection.close()
            except Exception:
                pass
        if process is not None and process.is_alive():
            process.join(timeout=2.0)
            if process.is_alive():
                process.terminate()
                process.join(timeout=1.0)
