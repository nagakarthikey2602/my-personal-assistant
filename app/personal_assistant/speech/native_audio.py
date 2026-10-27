"""Windows WinMM audio capture and playback using ctypes (no PortAudio/cffi).

Recording is only started when the user explicitly invokes push-to-talk. There is
no always-listening background capture in this build.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import threading
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np

WAVE_MAPPER = ctypes.c_uint(-1).value
WAVE_FORMAT_PCM = 1
CALLBACK_NULL = 0
WHDR_DONE = 0x00000001
WHDR_PREPARED = 0x00000002
MMSYSERR_NOERROR = 0
MAXPNAMELEN = 32

winmm = ctypes.WinDLL("winmm", use_last_error=True)


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class WAVEHDR(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.c_void_p),
        ("dwBufferLength", wintypes.DWORD),
        ("dwBytesRecorded", wintypes.DWORD),
        ("dwUser", ctypes.c_size_t),
        ("dwFlags", wintypes.DWORD),
        ("dwLoops", wintypes.DWORD),
        ("lpNext", ctypes.c_void_p),
        ("reserved", ctypes.c_size_t),
    ]


class WAVEINCAPSW(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("vDriverVersion", ctypes.c_uint),
        ("szPname", wintypes.WCHAR * MAXPNAMELEN),
        ("dwFormats", wintypes.DWORD),
        ("wChannels", wintypes.WORD),
        ("wReserved1", wintypes.WORD),
    ]


class WAVEOUTCAPSW(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("vDriverVersion", ctypes.c_uint),
        ("szPname", wintypes.WCHAR * MAXPNAMELEN),
        ("dwFormats", wintypes.DWORD),
        ("wChannels", wintypes.WORD),
        ("wReserved1", wintypes.WORD),
        ("dwSupport", wintypes.DWORD),
    ]


winmm.waveInGetNumDevs.restype = wintypes.UINT
winmm.waveInGetDevCapsW.argtypes = (ctypes.c_size_t, ctypes.POINTER(WAVEINCAPSW), wintypes.UINT)
winmm.waveInGetDevCapsW.restype = wintypes.UINT
winmm.waveOutGetNumDevs.restype = wintypes.UINT
winmm.waveOutGetDevCapsW.argtypes = (ctypes.c_size_t, ctypes.POINTER(WAVEOUTCAPSW), wintypes.UINT)
winmm.waveOutGetDevCapsW.restype = wintypes.UINT
winmm.waveInOpen.argtypes = (
    ctypes.POINTER(ctypes.c_void_p), wintypes.UINT, ctypes.POINTER(WAVEFORMATEX),
    ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD,
)
winmm.waveInOpen.restype = wintypes.UINT
winmm.waveInPrepareHeader.argtypes = (ctypes.c_void_p, ctypes.POINTER(WAVEHDR), wintypes.UINT)
winmm.waveInPrepareHeader.restype = wintypes.UINT
winmm.waveInUnprepareHeader.argtypes = (ctypes.c_void_p, ctypes.POINTER(WAVEHDR), wintypes.UINT)
winmm.waveInUnprepareHeader.restype = wintypes.UINT
winmm.waveInAddBuffer.argtypes = (ctypes.c_void_p, ctypes.POINTER(WAVEHDR), wintypes.UINT)
winmm.waveInAddBuffer.restype = wintypes.UINT
winmm.waveInStart.argtypes = (ctypes.c_void_p,)
winmm.waveInStart.restype = wintypes.UINT
winmm.waveInStop.argtypes = (ctypes.c_void_p,)
winmm.waveInStop.restype = wintypes.UINT
winmm.waveInReset.argtypes = (ctypes.c_void_p,)
winmm.waveInReset.restype = wintypes.UINT
winmm.waveInClose.argtypes = (ctypes.c_void_p,)
winmm.waveInClose.restype = wintypes.UINT

winmm.waveOutOpen.argtypes = (
    ctypes.POINTER(ctypes.c_void_p), wintypes.UINT, ctypes.POINTER(WAVEFORMATEX),
    ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD,
)
winmm.waveOutOpen.restype = wintypes.UINT
winmm.waveOutPrepareHeader.argtypes = (ctypes.c_void_p, ctypes.POINTER(WAVEHDR), wintypes.UINT)
winmm.waveOutPrepareHeader.restype = wintypes.UINT
winmm.waveOutUnprepareHeader.argtypes = (ctypes.c_void_p, ctypes.POINTER(WAVEHDR), wintypes.UINT)
winmm.waveOutUnprepareHeader.restype = wintypes.UINT
winmm.waveOutWrite.argtypes = (ctypes.c_void_p, ctypes.POINTER(WAVEHDR), wintypes.UINT)
winmm.waveOutWrite.restype = wintypes.UINT
winmm.waveOutReset.argtypes = (ctypes.c_void_p,)
winmm.waveOutReset.restype = wintypes.UINT
winmm.waveOutClose.argtypes = (ctypes.c_void_p,)
winmm.waveOutClose.restype = wintypes.UINT


_MM_ERRORS = {
    1: "unspecified error",
    2: "bad device ID",
    3: "device already allocated",
    4: "invalid handle",
    5: "no driver",
    6: "no device",
    7: "invalid parameter",
    8: "memory allocation failed",
    9: "device not supported",
    10: "bad format",
    11: "device in use",
    12: "wave header already prepared",
    32: "unsupported format",
    33: "device not enabled",
}


class AudioError(RuntimeError):
    pass


def _mm_error(code: int, operation: str) -> AudioError:
    return AudioError(f"{operation} failed: {_MM_ERRORS.get(code, f'WinMM error {code}')} (code {code})")


def list_input_devices() -> list[dict[str, Any]]:
    devices = []
    for index in range(int(winmm.waveInGetNumDevs())):
        caps = WAVEINCAPSW()
        result = winmm.waveInGetDevCapsW(index, ctypes.byref(caps), ctypes.sizeof(caps))
        if result != MMSYSERR_NOERROR:
            continue
        devices.append({
            "index": index,
            "name": caps.szPname.strip(),
            "kind": "input",
            "channels": int(caps.wChannels),
        })
    return devices


def list_output_devices() -> list[dict[str, Any]]:
    devices = []
    for index in range(int(winmm.waveOutGetNumDevs())):
        caps = WAVEOUTCAPSW()
        result = winmm.waveOutGetDevCapsW(index, ctypes.byref(caps), ctypes.sizeof(caps))
        if result != MMSYSERR_NOERROR:
            continue
        devices.append({
            "index": index,
            "name": caps.szPname.strip(),
            "kind": "output",
            "channels": int(caps.wChannels),
        })
    return devices


def list_audio_devices() -> dict[str, list[dict[str, Any]]]:
    return {"inputs": list_input_devices(), "outputs": list_output_devices()}


def _device_id(device_index: int | None, *, output: bool) -> int:
    if device_index is None:
        return WAVE_MAPPER
    count = int(winmm.waveOutGetNumDevs() if output else winmm.waveInGetNumDevs())
    if not isinstance(device_index, int) or not 0 <= device_index < count:
        raise AudioError(f"Audio device index {device_index!r} is not valid ({count} device(s) available).")
    return device_index


def _wave_format(sample_rate: int, channels: int = 1, bits: int = 16) -> WAVEFORMATEX:
    block = channels * bits // 8
    return WAVEFORMATEX(
        wFormatTag=WAVE_FORMAT_PCM,
        nChannels=channels,
        nSamplesPerSec=sample_rate,
        nAvgBytesPerSec=sample_rate * block,
        nBlockAlign=block,
        wBitsPerSample=bits,
        cbSize=0,
    )


def record_audio(
    max_seconds: float,
    sample_rate: int = 16000,
    device_index: int | None = None,
    stop_event: threading.Event | None = None,
) -> tuple[np.ndarray, int]:
    """Record mono PCM16 audio until stop_event is set or max_seconds elapses.

    The caller is responsible for visible user consent (push-to-talk button).
    """
    if not 0.2 <= max_seconds <= 60:
        raise AudioError("Recording duration must be between 0.2 and 60 seconds.")
    device = _device_id(device_index, output=False)
    errors: list[str] = []
    for rate in [sample_rate, 44100, 48000]:
        try:
            samples = _record_at_rate(max_seconds, rate, device, stop_event)
        except AudioError as exc:
            errors.append(str(exc))
            continue
        if rate != sample_rate:
            samples = resample(samples, rate, sample_rate)
        return samples, sample_rate
    raise AudioError("Could not record audio. " + " | ".join(errors[-2:]))


def _record_at_rate(max_seconds: float, sample_rate: int, device: int, stop_event: threading.Event | None) -> np.ndarray:
    fmt = _wave_format(sample_rate)
    handle = ctypes.c_void_p()
    result = winmm.waveInOpen(ctypes.byref(handle), device, ctypes.byref(fmt), None, 0, CALLBACK_NULL)
    if result != MMSYSERR_NOERROR:
        raise _mm_error(result, f"waveInOpen at {sample_rate} Hz")
    max_bytes = int(max_seconds * sample_rate * 2)
    buffer = (ctypes.c_char * max_bytes)()
    header = WAVEHDR(
        lpData=ctypes.cast(buffer, ctypes.c_void_p),
        dwBufferLength=max_bytes,
        dwBytesRecorded=0,
        dwUser=0,
        dwFlags=0,
        dwLoops=1,
        lpNext=None,
        reserved=0,
    )
    prepared = False
    try:
        result = winmm.waveInPrepareHeader(handle, ctypes.byref(header), ctypes.sizeof(header))
        if result != MMSYSERR_NOERROR:
            raise _mm_error(result, "waveInPrepareHeader")
        prepared = True
        result = winmm.waveInAddBuffer(handle, ctypes.byref(header), ctypes.sizeof(header))
        if result != MMSYSERR_NOERROR:
            raise _mm_error(result, "waveInAddBuffer")
        result = winmm.waveInStart(handle)
        if result != MMSYSERR_NOERROR:
            raise _mm_error(result, "waveInStart")
        started = time.monotonic()
        while time.monotonic() - started < max_seconds:
            if stop_event is not None and stop_event.wait(0.05):
                break
            if stop_event is None:
                time.sleep(0.05)
        winmm.waveInStop(handle)
        winmm.waveInReset(handle)
        recorded = int(header.dwBytesRecorded)
        if recorded <= 0:
            recorded = max_bytes if header.dwFlags & WHDR_DONE and not stop_event else 0
        raw = bytes(buffer[:min(recorded, max_bytes)])
        if len(raw) < 2:
            raise AudioError("The microphone returned no audio.")
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        return samples
    finally:
        if prepared:
            winmm.waveInUnprepareHeader(handle, ctypes.byref(header), ctypes.sizeof(header))
        if handle:
            winmm.waveInClose(handle)


def resample(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate or samples.size == 0:
        return samples.astype(np.float32, copy=False)
    duration = samples.size / float(source_rate)
    target_count = max(1, int(round(duration * target_rate)))
    source_positions = np.linspace(0.0, samples.size - 1, num=samples.size, dtype=np.float64)
    target_positions = np.linspace(0.0, samples.size - 1, num=target_count, dtype=np.float64)
    return np.interp(target_positions, source_positions, samples).astype(np.float32)


def save_wav(path: Path, samples: np.ndarray, sample_rate: int = 16000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return path


def read_wav_pcm16(path: Path) -> tuple[bytes, int, int, int]:
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    if width != 2:
        raise AudioError("Only 16-bit PCM WAV files are supported by the native playback path.")
    if not frames:
        raise AudioError("The WAV file contains no audio.")
    return frames, channels, rate, width


def play_wav(
    path: Path,
    device_index: int | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    frames, channels, rate, width = read_wav_pcm16(path)
    device = _device_id(device_index, output=True)
    fmt = _wave_format(rate, channels=channels, bits=width * 8)
    handle = ctypes.c_void_p()
    result = winmm.waveOutOpen(ctypes.byref(handle), device, ctypes.byref(fmt), None, 0, CALLBACK_NULL)
    if result != MMSYSERR_NOERROR:
        raise _mm_error(result, "waveOutOpen")
    buffer = (ctypes.c_char * len(frames)).from_buffer_copy(frames)
    header = WAVEHDR(
        lpData=ctypes.cast(buffer, ctypes.c_void_p),
        dwBufferLength=len(frames),
        dwBytesRecorded=0,
        dwUser=0,
        dwFlags=0,
        dwLoops=1,
        lpNext=None,
        reserved=0,
    )
    prepared = False
    try:
        result = winmm.waveOutPrepareHeader(handle, ctypes.byref(header), ctypes.sizeof(header))
        if result != MMSYSERR_NOERROR:
            raise _mm_error(result, "waveOutPrepareHeader")
        prepared = True
        result = winmm.waveOutWrite(handle, ctypes.byref(header), ctypes.sizeof(header))
        if result != MMSYSERR_NOERROR:
            raise _mm_error(result, "waveOutWrite")
        duration = len(frames) / float(rate * channels * width) + 2.0
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                winmm.waveOutReset(handle)
                break
            if header.dwFlags & WHDR_DONE:
                break
            time.sleep(0.03)
        else:
            winmm.waveOutReset(handle)
    finally:
        if prepared:
            winmm.waveOutUnprepareHeader(handle, ctypes.byref(header), ctypes.sizeof(header))
        if handle:
            winmm.waveOutClose(handle)


def audio_status() -> dict[str, Any]:
    inputs = list_input_devices()
    outputs = list_output_devices()
    return {
        "inputs": inputs,
        "outputs": outputs,
        "input_available": bool(inputs),
        "output_available": bool(outputs),
        "backend": "Windows WinMM (ctypes)",
    }
