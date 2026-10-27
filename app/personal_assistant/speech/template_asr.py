"""Closed-vocabulary speech recognizer trained from the user's own recordings.

Provenance: every template is a recording supplied by the user through
``scripts/record_voice_templates.py``. Features are computed locally with MFCC;
matching uses DTW. No pretrained acoustic model or cloud service is involved.

Limitation: this can only recognize phrases the user recorded. It is not a
general dictation engine. Arbitrary natural-language voice commands require a
larger user-trained acoustic/language model, which is not part of this milestone.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .features import dtw_distance, mfcc, similarity_from_distance
from .vad import trim_silence

logger = logging.getLogger(__name__)


@dataclass
class RecognitionCandidate:
    text: str
    similarity: float
    distance: float
    intent: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "similarity": round(self.similarity, 4),
            "distance": round(self.distance, 4),
            "intent": self.intent,
        }


@dataclass
class Transcription:
    text: str
    confidence: float
    alternatives: list[RecognitionCandidate] = field(default_factory=list)
    engine: str = "template-dtw"
    available: bool = True
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "confidence": round(self.confidence, 4),
            "alternatives": [item.to_dict() for item in self.alternatives],
            "engine": self.engine,
            "available": self.available,
            "reason": self.reason,
        }


class TemplateASR:
    def __init__(
        self,
        templates_dir: Path,
        *,
        sample_rate: int = 16000,
        threshold: float = 0.62,
        margin: float = 0.04,
        vad_threshold: float = 0.008,
    ) -> None:
        self.templates_dir = Path(templates_dir)
        self.sample_rate = int(sample_rate)
        self.threshold = float(threshold)
        self.margin = float(margin)
        self.vad_threshold = float(vad_threshold)
        self.manifest_path = self.templates_dir / "manifest.json"
        self.phrases: list[dict[str, Any]] = []
        self._features: list[tuple[str, str, str, np.ndarray]] = []
        self.load_error = ""
        self.load()

    def load(self) -> None:
        self.phrases = []
        self._features = []
        self.load_error = ""
        if not self.manifest_path.exists():
            self.load_error = "No user voice templates are installed."
            return
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            phrases = manifest.get("phrases", [])
            if not isinstance(phrases, list):
                raise ValueError("manifest phrases must be a list")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.load_error = f"Voice template manifest is unreadable: {exc.__class__.__name__}"
            return
        for phrase in phrases:
            if not isinstance(phrase, dict) or not phrase.get("text"):
                continue
            takes = phrase.get("takes", [])
            loaded = 0
            for take in takes:
                name = take.get("file") if isinstance(take, dict) else None
                if not name:
                    continue
                path = self.templates_dir / name
                try:
                    features = np.load(path, allow_pickle=False)
                    if not isinstance(features, np.ndarray) or features.ndim != 2:
                        raise ValueError("bad feature shape")
                    expected = take.get("sha256")
                    if expected and hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                        raise ValueError("checksum mismatch")
                except (OSError, ValueError) as exc:
                    logger.warning("Skipping voice template %s: %s", name, exc)
                    continue
                self._features.append((str(phrase["text"]), str(phrase.get("intent", "")), path.name, features))
                loaded += 1
            if loaded:
                self.phrases.append({**phrase, "loaded_takes": loaded})
        if not self._features:
            self.load_error = "No readable user voice templates were found."

    @property
    def available(self) -> bool:
        return bool(self._features)

    def status(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "engine": "template-dtw",
            "templates_dir": str(self.templates_dir),
            "phrases": [phrase.get("text") for phrase in self.phrases],
            "phrase_count": len(self.phrases),
            "take_count": len(self._features),
            "threshold": self.threshold,
            "margin": self.margin,
            "reason": self.load_error,
            "policy": "User-recorded templates; no pretrained weights; closed vocabulary only.",
        }

    def recognize(self, samples: np.ndarray, sample_rate: int | None = None) -> Transcription:
        if not self.available:
            return Transcription(
                text="",
                confidence=0.0,
                available=False,
                reason=self.load_error or "No voice templates are installed.",
            )
        rate = sample_rate or self.sample_rate
        if rate != self.sample_rate:
            from .native_audio import resample
            samples = resample(np.asarray(samples, dtype=np.float32), rate, self.sample_rate)
        vad = trim_silence(np.asarray(samples, dtype=np.float32), self.sample_rate, self.vad_threshold)
        if not vad.has_speech:
            return Transcription("", 0.0, available=True, reason=vad.reason)
        try:
            query = mfcc(vad.trimmed, self.sample_rate)
        except ValueError as exc:
            return Transcription("", 0.0, available=True, reason=str(exc))
        by_phrase: dict[str, dict[str, Any]] = {}
        for text, intent, file_name, features in self._features:
            distance = dtw_distance(query, features)
            similarity = similarity_from_distance(distance)
            current = by_phrase.get(text)
            if current is None or similarity > current["similarity"]:
                by_phrase[text] = {
                    "text": text,
                    "intent": intent,
                    "similarity": similarity,
                    "distance": distance,
                    "template": file_name,
                }
        ranked = sorted(by_phrase.values(), key=lambda item: item["similarity"], reverse=True)
        candidates = [
            RecognitionCandidate(item["text"], item["similarity"], item["distance"], item.get("intent", ""))
            for item in ranked
        ]
        if not candidates:
            return Transcription("", 0.0, available=True, reason="No candidate phrases were available.")
        best = candidates[0]
        if best.similarity < self.threshold:
            return Transcription(
                "",
                best.similarity,
                alternatives=candidates[:5],
                available=True,
                reason=(
                    f"Best candidate '{best.text}' was below the confidence threshold "
                    f"({best.similarity:.2f} < {self.threshold:.2f}). Please repeat or type the command."
                ),
            )
        if len(candidates) > 1 and (best.similarity - candidates[1].similarity) < self.margin:
            return Transcription(
                "",
                best.similarity,
                alternatives=candidates[:5],
                available=True,
                reason=(
                    f"Voice candidates were ambiguous: '{best.text}' vs '{candidates[1].text}'. "
                    "Please repeat or type the command."
                ),
            )
        return Transcription(
            best.text,
            best.similarity,
            alternatives=candidates[:5],
            available=True,
            reason="Recognized from user-recorded templates.",
        )
