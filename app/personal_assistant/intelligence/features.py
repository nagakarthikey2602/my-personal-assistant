"""Deterministic text features for the tiny local intent classifier."""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

import numpy as np

_WORD_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?", re.I)


def word_tokens(text: str) -> list[str]:
    return [match.group(0).lower() for match in _WORD_RE.finditer(text)]


def candidate_features(text: str) -> list[str]:
    words = word_tokens(text)
    features: list[str] = []
    for word in words:
        features.append("w:" + word)
        padded = f"<{word}>"
        for n in (3, 4):
            for index in range(0, max(1, len(padded) - n + 1)):
                features.append("c:" + padded[index:index + n])
    for first, second in zip(words, words[1:]):
        features.append("b:" + first + "_" + second)
    return features


def build_vocabulary(texts: Iterable[str], max_features: int = 4000) -> list[str]:
    counts: Counter[str] = Counter()
    for text in texts:
        counts.update(set(candidate_features(text)))
    # Prefer words and bigrams over character fragments when pruning.
    scored = sorted(
        counts.items(),
        key=lambda item: (
            0 if item[0].startswith("w:") else 1 if item[0].startswith("b:") else 2,
            -item[1],
            item[0],
        ),
    )
    vocabulary = [feature for feature, _count in scored[:max_features]]
    return sorted(vocabulary)


def vectorize(text: str, vocabulary: list[str]) -> np.ndarray:
    index = {feature: position for position, feature in enumerate(vocabulary)}
    vector = np.zeros(len(vocabulary), dtype=np.float64)
    for feature in set(candidate_features(text)):
        position = index.get(feature)
        if position is not None:
            vector[position] = 1.0
    norm = np.linalg.norm(vector)
    if norm > 0:
        vector /= norm
    return vector


def vectorize_many(texts: Iterable[str], vocabulary: list[str]) -> np.ndarray:
    rows = [vectorize(text, vocabulary) for text in texts]
    if not rows:
        return np.zeros((0, len(vocabulary)), dtype=np.float64)
    return np.vstack(rows)
