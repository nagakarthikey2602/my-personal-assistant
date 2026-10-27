"""Energy-based voice activity detection and recording quality checks.

This is a deterministic signal-processing component, not a learned model.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class VadResult:
    has_speech: bool
    speech_fraction: float
    duration_seconds: float
    peak: float
    rms: float
    trimmed: np.ndarray
    reason: str = ""


def frame_rms(samples: np.ndarray, frame_size: int) -> np.ndarray:
    if samples.size == 0:
        return np.array([], dtype=np.float32)
    count = max(1, int(np.ceil(samples.size / frame_size)))
    padded = np.zeros(count * frame_size, dtype=np.float32)
    padded[:samples.size] = samples
    frames = padded.reshape(count, frame_size)
    return np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)


def trim_silence(
    samples: np.ndarray,
    sample_rate: int,
    threshold: float = 0.008,
    *,
    frame_ms: int = 30,
    pre_ms: int = 150,
    post_ms: int = 300,
) -> VadResult:
    samples = np.asarray(samples, dtype=np.float32).flatten()
    duration = samples.size / float(sample_rate) if sample_rate else 0.0
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    rms = float(np.sqrt(np.mean(samples * samples) + 1e-12)) if samples.size else 0.0
    frame_size = max(1, int(sample_rate * frame_ms / 1000))
    energies = frame_rms(samples, frame_size)
    voiced = energies >= max(1e-6, float(threshold))
    speech_fraction = float(np.mean(voiced)) if voiced.size else 0.0
    if not voiced.any():
        return VadResult(False, speech_fraction, duration, peak, rms, np.array([], dtype=np.float32),
                         "No speech energy was detected above the configured threshold.")
    indices = np.flatnonzero(voiced)
    start_frame = max(0, int(indices[0]) - int(pre_ms / frame_ms))
    end_frame = min(len(energies), int(indices[-1]) + int(post_ms / frame_ms) + 1)
    start = max(0, start_frame * frame_size)
    end = min(samples.size, end_frame * frame_size)
    trimmed = samples[start:end].copy()
    if trimmed.size / sample_rate < 0.25:
        return VadResult(False, speech_fraction, duration, peak, rms, trimmed,
                         "The detected utterance is too short.")
    if peak < 0.002:
        return VadResult(False, speech_fraction, duration, peak, rms, trimmed,
                         "The recording is nearly silent; check the microphone and input level.")
    return VadResult(True, speech_fraction, trimmed.size / float(sample_rate), peak, rms, trimmed)


def quality_summary(result: VadResult) -> str:
    if not result.has_speech:
        return result.reason or "No usable speech was detected."
    return (
        f"Captured {result.duration_seconds:.1f}s of speech "
        f"(RMS {result.rms:.3f}, peak {result.peak:.2f})."
    )
