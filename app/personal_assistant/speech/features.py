"""From-scratch MFCC features and dynamic time warping.

Only numpy is used. There are no pretrained weights, no cloud calls, and no
third-party speech model code.
"""

from __future__ import annotations

import numpy as np


def pre_emphasis(samples: np.ndarray, coefficient: float = 0.97) -> np.ndarray:
    if samples.size == 0:
        return samples
    result = np.empty_like(samples)
    result[0] = samples[0]
    result[1:] = samples[1:] - coefficient * samples[:-1]
    return result


def _mel_from_hz(freq: np.ndarray | float) -> np.ndarray | float:
    return 2595.0 * np.log10(1.0 + np.asarray(freq) / 700.0)


def _hz_from_mel(mel: np.ndarray | float) -> np.ndarray | float:
    return 700.0 * (10.0 ** (np.asarray(mel) / 2595.0) - 1.0)


def mel_filterbank(sample_rate: int, n_fft: int, n_mels: int = 26, fmin: float = 20.0, fmax: float | None = None) -> np.ndarray:
    fmax = fmax or sample_rate / 2.0
    mel_points = np.linspace(_mel_from_hz(fmin), _mel_from_hz(fmax), n_mels + 2)
    hz_points = _hz_from_mel(mel_points)
    bins = np.floor((n_fft + 1) * hz_points / sample_rate).astype(int)
    filters = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float64)
    for index in range(n_mels):
        left, centre, right = bins[index], bins[index + 1], bins[index + 2]
        if centre == left:
            centre += 1
        if right == centre:
            right += 1
        left = max(0, min(left, filters.shape[1] - 1))
        centre = max(0, min(centre, filters.shape[1] - 1))
        right = max(0, min(right, filters.shape[1] - 1))
        for k in range(left, centre):
            filters[index, k] = (k - left) / max(1, centre - left)
        for k in range(centre, right):
            filters[index, k] = (right - k) / max(1, right - centre)
    return filters


def dct_matrix(n_filters: int, n_coefficients: int) -> np.ndarray:
    matrix = np.zeros((n_filters, n_coefficients), dtype=np.float64)
    for n in range(n_coefficients):
        for k in range(n_filters):
            matrix[k, n] = np.cos(np.pi * n * (k + 0.5) / n_filters)
    matrix[:, 0] *= np.sqrt(1.0 / n_filters)
    matrix[:, 1:] *= np.sqrt(2.0 / n_filters)
    return matrix


def _frames(samples: np.ndarray, frame_length: int, hop_length: int) -> np.ndarray:
    if samples.size < frame_length:
        samples = np.pad(samples, (0, frame_length - samples.size))
    count = 1 + (samples.size - frame_length) // hop_length
    shape = (count, frame_length)
    strides = (samples.strides[0] * hop_length, samples.strides[0])
    return np.lib.stride_tricks.as_strided(samples, shape=shape, strides=strides).copy()


def mfcc(
    samples: np.ndarray,
    sample_rate: int = 16000,
    *,
    n_mfcc: int = 13,
    n_mels: int = 26,
    frame_ms: int = 25,
    hop_ms: int = 10,
    use_deltas: bool = True,
) -> np.ndarray:
    samples = np.asarray(samples, dtype=np.float64).flatten()
    if samples.size == 0:
        raise ValueError("Cannot compute MFCC for empty audio.")
    peak = float(np.max(np.abs(samples)))
    if peak > 0:
        samples = samples / peak
    frame_length = max(16, int(sample_rate * frame_ms / 1000))
    hop_length = max(1, int(sample_rate * hop_ms / 1000))
    n_fft = 1
    while n_fft < frame_length:
        n_fft *= 2
    signal = pre_emphasis(samples)
    framed = _frames(signal, frame_length, hop_length)
    window = np.hamming(frame_length)
    framed = framed * window
    spectrum = np.fft.rfft(framed, n=n_fft, axis=1)
    power = (np.abs(spectrum) ** 2) / max(1, n_fft)
    filters = mel_filterbank(sample_rate, n_fft, n_mels=n_mels)
    energies = power @ filters.T
    energies = np.maximum(energies, 1e-10)
    log_energies = np.log(energies)
    coefficients = log_energies @ dct_matrix(n_mels, n_mfcc)[:, 1:n_mfcc + 1]
    if use_deltas and coefficients.shape[0] > 2:
        padded = np.pad(coefficients, ((2, 2), (0, 0)), mode="edge")
        deltas = (padded[4:] - padded[:-4]) / 4.0
        coefficients = np.concatenate([coefficients, deltas], axis=1)
    # Cepstral mean and variance normalisation makes templates robust to channel/gain differences.
    mean = coefficients.mean(axis=0, keepdims=True)
    std = coefficients.std(axis=0, keepdims=True) + 1e-8
    return ((coefficients - mean) / std).astype(np.float32)


def limit_frames(features: np.ndarray, maximum: int = 220) -> np.ndarray:
    if features.shape[0] <= maximum:
        return features
    indices = np.linspace(0, features.shape[0] - 1, num=maximum).astype(int)
    return features[indices]


def dtw_distance(a: np.ndarray, b: np.ndarray) -> float:
    a = limit_frames(np.asarray(a, dtype=np.float64))
    b = limit_frames(np.asarray(b, dtype=np.float64))
    if a.shape[1] != b.shape[1]:
        raise ValueError("MFCC feature dimensions do not match.")
    n, m = a.shape[0], b.shape[0]
    dimension = max(1, a.shape[1])
    cost = np.linalg.norm(a[:, None, :] - b[None, :, :], axis=2) / np.sqrt(dimension)
    accumulated = np.full((n + 1, m + 1), np.inf)
    accumulated[0, 0] = 0.0
    for i in range(1, n + 1):
        row = cost[i - 1]
        for j in range(1, m + 1):
            accumulated[i, j] = row[j - 1] + min(
                accumulated[i - 1, j],
                accumulated[i, j - 1],
                accumulated[i - 1, j - 1],
            )
    return float(accumulated[n, m] / (n + m))


def similarity_from_distance(distance: float) -> float:
    return float(1.0 / (1.0 + max(0.0, distance)))
