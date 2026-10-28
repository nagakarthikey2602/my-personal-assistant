"""Normalise free-form speech transcripts into the assistant's command grammar.

Vosk returns lowercase text without punctuation. Spoken punctuation and common
word splits are converted conservatively here. This module never executes
anything; it only prepares text for the deterministic parser.
"""

from __future__ import annotations

import re
from typing import Iterable

_DOT_MARKER = "\u0001"
DEFAULT_WAKE_ALIASES = ("hardy", "harty", "heart y")

_PUNCTUATION_WORDS: tuple[tuple[str, str], ...] = (
    (r"\bquestion\s+mark\b", "? "),
    (r"\bexclamation\s+(?:mark|point)\b", "! "),
    (r"\b(?:full\s+stop|period)\b", ". "),
    (r"\bcomma\b", ", "),
    (r"\b(?:semicolon|semi\s+colon)\b", "; "),
    (r"\b(?:colon|coal\s+on)\b", ":"),
    (r"\b(?:underscore|under\s+score)\b", "_"),
    (r"\b(?:backslash|back\s+slash)\b", "\\"),
    (r"\b(?:slash|forward\s+slash)\b", "/"),
    (r"\b(?:hyphen|dash)\b", "-"),
    (r"\bdot\b", _DOT_MARKER),
    (r"\bat\s+sign\b", "@"),
)

_WORD_JOINS: tuple[tuple[str, str], ...] = (
    (r"\bnote\s*pad\b", "notepad"),
    (r"\bscreen\s*shot\b", "screenshot"),
    (r"\bwi[\s-]?fi\b", "wifi"),
    (r"\bdown\s*loads\b", "downloads"),
    (r"\bdown\s*load\b", "download"),
    (r"\bpower\s*point\b", "powerpoint"),
    (r"\bgit\s*hub\b", "github"),
    (r"\byou\s*tube\b", "youtube"),
    (r"\bvs\s*code\b", "vs code"),
    (r"\bcontrol\s+panel\b", "control panel"),
    (r"\btask\s+manager\b", "task manager"),
    (r"\bphoto\s*shop\b", "photoshop"),
)

_POLITE_PREFIXES = (
    r"please\s+",
    r"kindly\s+",
    r"(?:can|could|would|will)\s+you\s+",
    r"(?:i\s+want\s+you\s+to|i\s+need\s+you\s+to)\s+",
    r"(?:go\s+ahead\s+and)\s+",
)


def strip_unknown(text: str) -> str:
    """Remove Vosk's ``[unk]`` placeholder and collapse the gaps it leaves."""
    value = re.sub(r"\[\s*unk\s*\]", " ", str(text or ""), flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip()


def normalize_spoken_text(text: str) -> str:
    """Lowercase, collapse whitespace, and expand spoken punctuation."""
    value = strip_unknown(text).lower()
    value = re.sub(r"\s+", " ", value)
    for pattern, replacement in _PUNCTUATION_WORDS:
        value = re.sub(pattern, lambda _match, text=replacement: text, value)
    for pattern, replacement in _WORD_JOINS:
        value = re.sub(pattern, lambda _match, text=replacement: text, value)
    value = re.sub(r"\s+([.,:;!?])", r"\1", value)
    value = re.sub(r"\s*\u0001\s*", ".", value)
    return re.sub(r"\s+", " ", value).strip()


def clean_command(text: str) -> str:
    """Remove filler phrases from a spoken command without changing its intent."""
    value = normalize_spoken_text(text)
    changed = True
    while changed and value:
        changed = False
        for pattern in _POLITE_PREFIXES:
            updated = re.sub(rf"^(?:{pattern})", "", value, count=1)
            if updated != value:
                value = updated.strip()
                changed = True
    return value.strip(" ,.")


def wake_aliases(wake_word: str, aliases: Iterable[str] | None = None) -> list[str]:
    values = [str(wake_word or "").strip().lower()]
    if aliases is None:
        values.extend(DEFAULT_WAKE_ALIASES)
    elif aliases:
        values.extend(str(item).strip().lower() for item in aliases if str(item).strip())
    unique: list[str] = []
    for item in values:
        cleaned = re.sub(r"[^a-z0-9 ]+", " ", item)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if cleaned and cleaned not in unique:
            unique.append(cleaned)
    return sorted(unique, key=len, reverse=True)


def split_wake_command(
    text: str,
    wake_word: str = "hearty",
    aliases: Iterable[str] | None = None,
) -> tuple[bool, str]:
    """Return ``(wake_word_heard, command_after_wake_word)``.

    The wake word must be at the start of the utterance. Optional greetings such
    as "hey" and "okay" are accepted before it.
    """
    value = normalize_spoken_text(text)
    for alias in wake_aliases(wake_word, aliases):
        prefixes = [alias, f"hey {alias}", f"hi {alias}", f"ok {alias}", f"okay {alias}"]
        for prefix in prefixes:
            if value == prefix:
                return True, ""
            if value.startswith(prefix + " "):
                return True, value[len(prefix):].strip(" ,.")
    return False, value


def find_wake_command(
    text: str,
    wake_word: str = "hearty",
    aliases: Iterable[str] | None = None,
) -> tuple[bool, str]:
    """Find a wake word anywhere in a grammar-constrained transcript.

    A constrained Vosk recognizer often emits leading ``[unk]`` tokens for
    background noise before the wake word, for example ``[unk] hearty status``.
    Requiring the wake word at index zero would reject that result, so this
    helper strips the placeholders and then looks for the first whole-word alias.
    """
    value = normalize_spoken_text(text)
    words = wake_aliases(wake_word, aliases)
    heard, command = split_wake_command(value, wake_word=wake_word, aliases=aliases)
    if heard:
        return True, _stop_at_next_wake(command, words)
    for alias in words:
        match = re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", value)
        if match:
            command = value[match.end():].strip(" ,.")
            return True, _stop_at_next_wake(command, words)
    return False, value


def _stop_at_next_wake(command: str, words: Iterable[str]) -> str:
    # A single constrained result can contain several wake phrases when the
    # speaker does not pause. Keep only the first command so the parser never
    # receives two unrelated requests at once.
    for other in words:
        following = re.search(rf"(?<![a-z0-9]){re.escape(other)}(?![a-z0-9])", command)
        if following:
            command = command[:following.start()].strip(" ,.")
    return command


def prepare_voice_command(
    text: str,
    wake_word: str = "hearty",
    aliases: Iterable[str] | None = None,
) -> tuple[bool, str]:
    """Split the wake word and clean a command for the deterministic parser."""
    heard, command = split_wake_command(text, wake_word=wake_word, aliases=aliases)
    return heard, clean_command(command)
