"""Offline text-to-speech.

Default: a deterministic rule-based formant synthesizer implemented in this
repository. It has no learned weights and makes no network calls. Its
intelligibility has not been perceptually validated; the UI labels it as robotic.

Optional: Windows SAPI voices are Microsoft-pretrained models. They are disabled
by policy and can only be enabled by explicit configuration acknowledgement.
There is no automatic fallback from the from-scratch synthesizer to SAPI.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import numpy as np

from ..config import Config
from .native_audio import AudioError, play_wav, save_wav

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000

# Formant targets in Hz (F1, F2, F3) and nominal durations in seconds.
_VOWELS: dict[str, tuple[tuple[float, float, float], float]] = {
    "IY": ((270, 2290, 3010), 0.13),
    "IH": ((390, 1990, 2550), 0.11),
    "EH": ((530, 1840, 2480), 0.12),
    "AE": ((660, 1720, 2410), 0.14),
    "AA": ((730, 1090, 2440), 0.15),
    "AO": ((570, 840, 2410), 0.13),
    "UH": ((440, 1020, 2240), 0.11),
    "UW": ((300, 870, 2240), 0.12),
    "AH": ((640, 1190, 2390), 0.10),
    "ER": ((490, 1350, 1690), 0.12),
}
_DIPHTHONGS: dict[str, tuple[str, str, float]] = {
    "EY": ("EH", "IY", 0.18),
    "AY": ("AA", "IY", 0.20),
    "OW": ("AO", "UW", 0.18),
    "AW": ("AA", "UW", 0.20),
    "OY": ("AO", "IY", 0.20),
}
_CONSONANTS: dict[str, dict[str, Any]] = {
    "P": {"type": "stop", "voiced": False, "formants": (400, 1100, 2400), "duration": 0.09, "noise": 2500},
    "B": {"type": "stop", "voiced": True, "formants": (300, 1000, 2200), "duration": 0.09, "noise": 1800},
    "T": {"type": "stop", "voiced": False, "formants": (500, 1700, 2600), "duration": 0.09, "noise": 4000},
    "D": {"type": "stop", "voiced": True, "formants": (350, 1600, 2500), "duration": 0.09, "noise": 3000},
    "K": {"type": "stop", "voiced": False, "formants": (500, 2000, 2800), "duration": 0.09, "noise": 3500},
    "G": {"type": "stop", "voiced": True, "formants": (350, 1900, 2600), "duration": 0.09, "noise": 2800},
    "F": {"type": "fricative", "voiced": False, "formants": (500, 1500, 2500), "duration": 0.09, "noise": 5000, "bandwidth": 1200},
    "V": {"type": "fricative", "voiced": True, "formants": (350, 1200, 2300), "duration": 0.08, "noise": 4000, "bandwidth": 1000},
    "TH": {"type": "fricative", "voiced": False, "formants": (400, 1600, 2600), "duration": 0.08, "noise": 5500, "bandwidth": 1800},
    "DH": {"type": "fricative", "voiced": True, "formants": (300, 1400, 2500), "duration": 0.07, "noise": 4500, "bandwidth": 1500},
    "S": {"type": "fricative", "voiced": False, "formants": (500, 1800, 2800), "duration": 0.10, "noise": 6500, "bandwidth": 2500},
    "Z": {"type": "fricative", "voiced": True, "formants": (350, 1600, 2700), "duration": 0.09, "noise": 6000, "bandwidth": 2200},
    "SH": {"type": "fricative", "voiced": False, "formants": (500, 2000, 3000), "duration": 0.10, "noise": 4500, "bandwidth": 3000},
    "ZH": {"type": "fricative", "voiced": True, "formants": (350, 1800, 2800), "duration": 0.09, "noise": 4000, "bandwidth": 2600},
    "HH": {"type": "fricative", "voiced": False, "formants": (500, 1500, 2500), "duration": 0.07, "noise": 2000, "bandwidth": 2500},
    "CH": {"type": "affricate", "voiced": False, "formants": (500, 1800, 2600), "duration": 0.12, "noise": 4500},
    "JH": {"type": "affricate", "voiced": True, "formants": (350, 1800, 2500), "duration": 0.12, "noise": 3800},
    "M": {"type": "nasal", "voiced": True, "formants": (250, 1000, 2200), "duration": 0.08, "noise": 0},
    "N": {"type": "nasal", "voiced": True, "formants": (300, 1500, 2500), "duration": 0.07, "noise": 0},
    "NG": {"type": "nasal", "voiced": True, "formants": (300, 1800, 2300), "duration": 0.08, "noise": 0},
    "L": {"type": "liquid", "voiced": True, "formants": (380, 1000, 2600), "duration": 0.07, "noise": 0},
    "R": {"type": "liquid", "voiced": True, "formants": (420, 1200, 1600), "duration": 0.08, "noise": 0},
    "W": {"type": "glide", "voiced": True, "formants": (300, 700, 2200), "duration": 0.06, "noise": 0},
    "Y": {"type": "glide", "voiced": True, "formants": (280, 2000, 2800), "duration": 0.06, "noise": 0},
}

_LEXICON = {
    "a": "AH", "an": "AE N", "the": "DH AH", "to": "T UW", "and": "AE N D",
    "of": "AH V", "in": "IH N", "on": "AA N", "is": "IH Z", "it": "IH T",
    "you": "Y UW", "i": "AY", "me": "M IY", "my": "M AY", "we": "W IY",
    "our": "AW ER", "open": "OW P AH N", "close": "K L OW Z", "file": "F AY L",
    "folder": "F OW L D ER", "memory": "M EH M ER IY", "system": "S IH S T AH M",
    "status": "S T AE T AH S", "help": "HH EH L P", "stop": "S T AA P",
    "cancel": "K AE N S AH L", "undo": "AH N D UW", "search": "S ER CH",
    "create": "K R IY EY T", "move": "M UW V", "copy": "K AA P IY",
    "delete": "D IH L IY T", "pdf": "P IY D IY EH F", "documents": "D AA K Y UW M EH N T S",
    "downloads": "D AW N L OW D Z", "notepad": "N OW T P AE D", "calculator": "K AE L K Y AH L EY T ER",
    "window": "W IH N D OW", "screenshot": "S K R IY N SH AA T", "voice": "V OY S",
    "microphone": "M AY K R OW F OW N", "assistant": "AH S IH S T AH N T", "error": "EH R ER",
    "failed": "F EY L D", "complete": "K AH M P L IY T", "completed": "K AH M P L IY T IH D",
    "task": "T AE S K", "test": "T EH S T", "tests": "T EH S T S", "local": "L OW K AH L",
    "offline": "AO F L AY N", "model": "M AA D AH L", "speech": "S P IY CH",
    "please": "P L IY Z", "sorry": "S AA R IY", "cannot": "K AE N AA T", "not": "N AA T",
    "now": "N AW", "what": "W AH T", "time": "T AY M", "list": "L IH S T",
    "process": "P R AA S EH S", "processes": "P R AA S EH S IH Z", "started": "S T AA R T IH D",
    "launch": "L AO N CH", "launched": "L AO N CH T", "running": "R AH N IH NG",
    "verified": "V EH R AH F AY D", "unverified": "AH N V EH R AH F AY D",
    "warning": "W AO R N IH NG", "never": "N EH V ER", "always": "AO L W EY Z",
    "yes": "Y EH S", "no": "N OW", "okay": "OW K EY", "hello": "HH AH L OW",
    "there": "DH EH R", "are": "AA R", "was": "W AH Z", "were": "W ER",
    "have": "HH AE V", "has": "HH AE Z", "can": "K AE N", "will": "W IH L",
    "found": "F AW N D", "folder.": "F OW L D ER", "operation": "AA P ER EY SH AH N",
    "zero": "Z IH R OW", "one": "W AH N", "two": "T UW", "three": "TH R IY",
    "four": "F AO R", "five": "F AY V", "six": "S IH K S", "seven": "S EH V AH N",
    "eight": "EY T", "nine": "N AY N", "ten": "T EH N", "eleven": "IH L EH V AH N",
    "twelve": "T W EH L V", "thirteen": "TH ER T IY N", "fourteen": "F AO R T IY N",
    "fifteen": "F IH F T IY N", "sixteen": "S IH K S T IY N", "seventeen": "S EH V AH N T IY N",
    "eighteen": "EY T IY N", "nineteen": "N AY N T IY N", "twenty": "T W EH N T IY",
    "thirty": "TH ER T IY", "forty": "F AO R T IY", "fifty": "F IH F T IY",
    "sixty": "S IH K S T IY", "seventy": "S EH V AH N T IY", "eighty": "EY T IY",
    "ninety": "N AY N T IY", "hundred": "HH AH N D R AH D", "thousand": "TH AW Z AH N D",
}
_PHONEMES = set(_VOWELS) | set(_DIPHTHONGS) | set(_CONSONANTS)

_GRAPHEMES = [
    ("ough", "AO"), ("augh", "AO"), ("tion", "SH AH N"), ("sion", "ZH AH N"),
    ("tch", "CH"), ("dge", "JH"), ("igh", "AY"), ("eigh", "EY"),
    ("ch", "CH"), ("sh", "SH"), ("th", "TH"), ("ph", "F"), ("wh", "W"),
    ("ck", "K"), ("ng", "NG"), ("qu", "K W"), ("gh", ""),
    ("ee", "IY"), ("ea", "IY"), ("oo", "UW"), ("ou", "AW"), ("ow", "OW"),
    ("oi", "OY"), ("oy", "OY"), ("ai", "EY"), ("ay", "EY"), ("oa", "OW"),
    ("au", "AO"), ("aw", "AO"), ("ei", "EY"), ("ey", "EY"), ("ie", "IY"),
    ("ue", "UW"), ("ui", "UW"), ("ar", "AA R"), ("er", "ER"), ("ir", "ER"),
    ("or", "AO R"), ("ur", "ER"),
    ("a", "AE"), ("e", "EH"), ("i", "IH"), ("o", "AA"), ("u", "AH"),
    ("b", "B"), ("c", "K"), ("d", "D"), ("f", "F"), ("g", "G"), ("h", "HH"),
    ("j", "JH"), ("k", "K"), ("l", "L"), ("m", "M"), ("n", "N"), ("p", "P"),
    ("q", "K"), ("r", "R"), ("s", "S"), ("t", "T"), ("v", "V"), ("w", "W"),
    ("x", "K S"), ("y", "Y"), ("z", "Z"),
]
_GRAPHEME_LOOKUP = dict(_GRAPHEMES)

_DIGITS = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
    "10": "ten", "11": "eleven", "12": "twelve", "13": "thirteen", "14": "fourteen",
    "15": "fifteen", "16": "sixteen", "17": "seventeen", "18": "eighteen", "19": "nineteen",
    "20": "twenty", "30": "thirty", "40": "forty", "50": "fifty", "60": "sixty",
    "70": "seventy", "80": "eighty", "90": "ninety",
}


def _spell_word(word: str) -> str:
    lower = word.lower()
    if lower in _LEXICON:
        return _LEXICON[lower]
    # Magic-e: make the preceding vowel long and drop the final e.
    if len(lower) >= 3 and lower.endswith("e") and lower[-2] not in "aeiou" and lower[-3] in "aeiou":
        long_vowel = {"a": "EY", "e": "IY", "i": "AY", "o": "OW", "u": "UW"}[lower[-3]]
        prefix = _spell_word_simple(lower[:-3]) if len(lower) > 3 else ""
        final_consonant = _spell_word_simple(lower[-2])
        return f"{prefix} {long_vowel} {final_consonant}".strip()
    return _spell_word_simple(lower)


def _spell_word_simple(word: str) -> str:
    if word in _LEXICON:
        return _LEXICON[word]
    output: list[str] = []
    index = 0
    while index < len(word):
        if word[index] in "aeiou" and index + 2 < len(word) and word[index + 1] in "bcdfghjklmnpqrstvwxyz" and word[index + 2] == "e" and index + 3 == len(word):
            output.append({"a": "EY", "e": "IY", "i": "AY", "o": "OW", "u": "UW"}[word[index]])
            index += 3
            continue
        if word[index] == "c" and index + 1 < len(word) and word[index + 1] in "eiy":
            output.append("S")
            index += 1
            continue
        if word[index] == "g" and index + 1 < len(word) and word[index + 1] in "eiy":
            output.append("JH")
            index += 1
            continue
        matched = False
        for length in (4, 3, 2, 1):
            piece = word[index:index + length]
            if piece in _GRAPHEME_LOOKUP:
                output.append(_GRAPHEME_LOOKUP[piece])
                index += length
                matched = True
                break
        if not matched:
            index += 1
    # Collapse duplicated consonant phonemes caused by double letters.
    cleaned: list[str] = []
    for phoneme in output:
        if cleaned and cleaned[-1] == phoneme and phoneme not in _VOWELS and phoneme not in _DIPHTHONGS:
            continue
        cleaned.append(phoneme)
    return " ".join(item for item in cleaned if item)


def text_to_phonemes(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in re.findall(r"[A-Za-z0-9']+|[.,!?;:]", text):
        if raw in ".,!?;:":
            tokens.append("PAUSE_LONG" if raw in ".!?" else "PAUSE_SHORT")
            continue
        word = raw.strip("'").lower()
        if not word:
            continue
        if word.isdigit():
            if word in _DIGITS:
                word = _DIGITS[word]
            else:
                word = " ".join(_DIGITS.get(digit, digit) for digit in word)
        phones = _spell_word(word)
        tokens.extend(phone.split())
        tokens.append("PAUSE_WORD")
    return tokens


def _resonator(signal: np.ndarray, frequency: float, bandwidth: float, sample_rate: int) -> np.ndarray:
    n = len(signal)
    if n == 0 or frequency <= 0:
        return np.zeros(n)
    r = float(np.exp(-np.pi * bandwidth / sample_rate))
    theta = 2.0 * np.pi * frequency / sample_rate
    a1 = 2.0 * r * np.cos(theta)
    a2 = -r * r
    gain = (1.0 - r) * np.sqrt(max(1e-6, 1.0 - 2.0 * r * np.cos(2.0 * theta) + r * r))
    output = np.zeros(n)
    y1 = y2 = 0.0
    for i in range(n):
        value = gain * signal[i] + a1 * y1 + a2 * y2
        y2, y1 = y1, value
        output[i] = value
    return output


def _voiced_source(count: int, f0: float, sample_rate: int) -> np.ndarray:
    source = np.zeros(count, dtype=np.float64)
    period = max(2.0, sample_rate / max(60.0, f0))
    position = 0.0
    while position < count:
        index = int(position)
        if index < count:
            source[index] += 1.0
        position += period
    # A little breath noise keeps the excitation from sounding perfectly synthetic.
    source += np.random.default_rng(7).normal(0.0, 0.012, count)
    return source


def _render_segment(formants: tuple[float, float, float], duration: float, sample_rate: int, *, voiced: bool, noise: bool = False, noise_freq: float = 3000.0, noise_bandwidth: float = 1800.0) -> np.ndarray:
    count = max(1, int(duration * sample_rate))
    if voiced:
        source = _voiced_source(count, 120.0 if not noise else 100.0, sample_rate)
    else:
        source = np.random.default_rng(11).normal(0.0, 0.25, count)
    if noise:
        fricative = np.random.default_rng(13).normal(0.0, 0.35, count)
        fricative = _resonator(fricative, noise_freq, noise_bandwidth, sample_rate)
        source = source + 1.4 * fricative if voiced else 1.6 * fricative
    output = (
        1.0 * _resonator(source, formants[0], 70.0, sample_rate)
        + 0.55 * _resonator(source, formants[1], 110.0, sample_rate)
        + 0.25 * _resonator(source, formants[2], 160.0, sample_rate)
    )
    # Segment envelope: short attack/release avoids clicks.
    attack = min(count // 4, int(0.010 * sample_rate))
    release = min(count // 3, int(0.018 * sample_rate))
    envelope = np.ones(count)
    if attack > 0:
        envelope[:attack] = np.linspace(0.0, 1.0, attack)
    if release > 0:
        envelope[-release:] = np.linspace(1.0, 0.0, release)
    return output * envelope


def _render_tokens(tokens: list[str], sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    pieces: list[np.ndarray] = []
    previous_vowel: tuple[float, float, float] | None = None
    for token in tokens:
        if token == "PAUSE_WORD":
            pieces.append(np.zeros(int(0.045 * sample_rate)))
            previous_vowel = None
            continue
        if token == "PAUSE_SHORT":
            pieces.append(np.zeros(int(0.14 * sample_rate)))
            previous_vowel = None
            continue
        if token == "PAUSE_LONG":
            pieces.append(np.zeros(int(0.32 * sample_rate)))
            previous_vowel = None
            continue
        if token in _VOWELS:
            formants, duration = _VOWELS[token]
            pieces.append(_render_segment(formants, duration, sample_rate, voiced=True))
            previous_vowel = formants
            continue
        if token in _DIPHTHONGS:
            first, second, duration = _DIPHTHONGS[token]
            start = _VOWELS[first][0]
            end = _VOWELS[second][0]
            count = int(duration * sample_rate)
            source = _voiced_source(count, 118.0, sample_rate)
            output = np.zeros(count)
            for part in range(3):
                interpolation = np.linspace(start[part], end[part], count)
                # A time-varying formant is approximated in four sub-segments.
                segment_out = np.zeros(count)
                splits = 4
                for index in range(splits):
                    begin = index * count // splits
                    finish = (index + 1) * count // splits
                    frequency = float(np.mean(interpolation[begin:finish]))
                    segment_out[begin:finish] = _resonator(
                        source[begin:finish], frequency, 90.0 + part * 30, sample_rate
                    )
                output += segment_out * (1.0 if part == 0 else 0.55 if part == 1 else 0.25)
            pieces.append(output * 0.8)
            previous_vowel = end
            continue
        spec = _CONSONANTS.get(token)
        if spec is None:
            continue
        formants = spec["formants"]
        kind = spec["type"]
        if kind == "stop":
            pieces.append(np.zeros(int(0.025 * sample_rate)))
            pieces.append(_render_segment(formants, 0.025, sample_rate, voiced=spec["voiced"], noise=True, noise_freq=spec.get("noise", 2500), noise_bandwidth=1200))
        elif kind == "affricate":
            pieces.append(np.zeros(int(0.02 * sample_rate)))
            pieces.append(_render_segment(formants, 0.05, sample_rate, voiced=spec["voiced"], noise=True, noise_freq=spec.get("noise", 4500), noise_bandwidth=1200))
            pieces.append(_render_segment(formants, 0.055, sample_rate, voiced=spec["voiced"], noise=True, noise_freq=spec.get("noise", 4500) * 0.6, noise_bandwidth=2500))
        elif kind == "fricative":
            pieces.append(_render_segment(formants, spec["duration"], sample_rate, voiced=spec["voiced"], noise=True, noise_freq=spec.get("noise", 4000), noise_bandwidth=spec.get("bandwidth", 1800)))
        else:
            pieces.append(_render_segment(formants, spec["duration"], sample_rate, voiced=spec["voiced"]))
            previous_vowel = formants
    if not pieces:
        pieces.append(np.zeros(int(0.2 * sample_rate)))
    audio = np.concatenate(pieces)
    if audio.size == 0:
        audio = np.zeros(int(0.2 * sample_rate))
    # Gentle smoothing and peak normalisation; never upload or transform externally.
    kernel = np.ones(5) / 5.0
    audio = np.convolve(audio, kernel, mode="same")
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 0:
        audio = audio / peak * 0.65
    fade = min(audio.size, int(0.015 * sample_rate))
    if fade > 1:
        audio[:fade] *= np.linspace(0.0, 1.0, fade)
        audio[-fade:] *= np.linspace(1.0, 0.0, fade)
    return audio.astype(np.float32)


class FormantSynthesizer:
    name = "formant-rule-based"
    pretrained = False

    def synthesize(self, text: str, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
        clean = re.sub(r"\s+", " ", text).strip()
        if not clean:
            raise ValueError("No text was provided for speech synthesis.")
        if len(clean) > 2000:
            raise ValueError("Speech text is limited to 2,000 characters per utterance.")
        tokens = text_to_phonemes(clean)
        if not any(token in _PHONEMES for token in tokens):
            raise ValueError("The text could not be converted to speech sounds.")
        return _render_tokens(tokens, sample_rate)

    def status(self) -> dict[str, Any]:
        return {
            "engine": self.name,
            "available": True,
            "pretrained": False,
            "policy": "Deterministic signal synthesis in this repository; no learned weights.",
            "quality": "Robotic and not perceptually validated. Optional OS voices require explicit policy acknowledgement.",
        }


class SapiSynthesizer:
    name = "windows-sapi-opt-in"
    pretrained = True

    def __init__(self, config: Config) -> None:
        if not config.get("speech.allow_os_pretrained_tts"):
            raise RuntimeError("SAPI voices are disabled by the model policy.")
        self.script = Path(__file__).with_name("sapi_tts.ps1")
        self.temp = config.path("paths.temp") / "sapi"

    def synthesize_to_file(self, text: str) -> Path:
        if len(text) > 2000:
            raise ValueError("Speech text is limited to 2,000 characters per utterance.")
        self.temp.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex[:10]
        text_path = self.temp / f"text_{token}.txt"
        wav_path = self.temp / f"speech_{token}.wav"
        text_path.write_text(text, encoding="utf-8")
        try:
            completed = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", str(self.script), "-TextPath", str(text_path), "-WavPath", str(wav_path),
                ],
                capture_output=True,
                timeout=60,
                shell=False,
                creationflags=0x08000000,
            )
            if completed.returncode != 0 or not wav_path.exists():
                raise RuntimeError("SAPI synthesis failed.")
            return wav_path
        finally:
            try:
                text_path.unlink(missing_ok=True)
            except OSError:
                pass

    def status(self) -> dict[str, Any]:
        return {
            "engine": self.name,
            "available": True,
            "pretrained": True,
            "policy": "Uses Microsoft-pretrained OS voices after explicit user acknowledgement.",
        }


class SpeechOutput:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.temp = config.path("paths.temp") / "tts"
        self.temp.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._current_stop: threading.Event | None = None
        engine = str(config.get("speech.tts_engine", "formant"))
        self.error = ""
        self.backend: FormantSynthesizer | SapiSynthesizer | None = None
        if engine == "formant":
            self.backend = FormantSynthesizer()
        elif engine == "sapi":
            if config.get("speech.allow_os_pretrained_tts"):
                self.backend = SapiSynthesizer(config)
            else:
                self.error = "SAPI is disabled until the model-policy acknowledgement is enabled."
        elif engine == "none":
            self.error = "Speech output is disabled in configuration."
        else:
            self.error = f"Unknown speech engine: {engine}"
        self._cleanup_old()

    def _cleanup_old(self) -> None:
        cutoff = time.time() - 86400
        try:
            for path in self.temp.glob("*.wav"):
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
        except OSError:
            pass

    def status(self) -> dict[str, Any]:
        return {
            "configured_engine": self.config.get("speech.tts_engine"),
            "available": self.backend is not None,
            "backend": self.backend.status() if self.backend else None,
            "error": self.error,
            "speaking": self._current_stop is not None,
        }

    def stop(self) -> None:
        with self._lock:
            self._stop_event.set()
            if self._current_stop is not None:
                self._current_stop.set()

    def speak(self, text: str, *, stop_event: threading.Event | None = None) -> Path:
        if self.backend is None:
            raise AudioError(self.error or "Speech output is unavailable.")
        text = text.strip()
        if not text:
            raise AudioError("No text was provided for speech output.")
        with self._lock:
            self._stop_event.clear()
            local_stop = threading.Event()
            self._current_stop = local_stop
        wav_path: Path | None = None
        try:
            if isinstance(self.backend, SapiSynthesizer):
                wav_path = self.backend.synthesize_to_file(text)
            else:
                samples = self.backend.synthesize(text)
                wav_path = self.temp / f"speech_{uuid.uuid4().hex[:10]}.wav"
                save_wav(wav_path, samples, SAMPLE_RATE)
            if stop_event is not None and stop_event.is_set():
                return wav_path
            if local_stop.is_set():
                return wav_path
            play_wav(
                wav_path,
                device_index=self.config.get("speech.output_device"),
                stop_event=local_stop if stop_event is None else _combined_stop(local_stop, stop_event),
            )
            return wav_path
        finally:
            with self._lock:
                self._current_stop = None
            try:
                if wav_path is not None:
                    wav_path.unlink(missing_ok=True)
            except OSError:
                pass


class _combined_stop:
    """Event-like adapter that is set when either underlying event is set."""

    def __init__(self, first: threading.Event, second: threading.Event) -> None:
        self.first = first
        self.second = second

    def is_set(self) -> bool:
        return self.first.is_set() or self.second.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        deadline = time.monotonic() + (timeout or 0)
        while True:
            if self.is_set():
                return True
            if timeout is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
