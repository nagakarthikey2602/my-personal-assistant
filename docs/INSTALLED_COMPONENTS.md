# Installed components and provenance

This document records what was actually installed and exercised for source build
`0.1.0`. It is an environment record, not a promise that a different machine has
the same files. Hashes are local integrity aids; they are **not** signatures or
remote attestation.

Recorded on **2026-10-27** on Windows 11 Home `10.0.26300` (64-bit), CPython
**3.14.6** (`MSC v.1944`, 64-bit), in the project virtual environment at
`.venv`.

## Application, model, and dataset artifacts

| Artifact | Path | Bytes | SHA-256 |
|---|---|---:|---|
| Trained intent model | `models/intent_model.json` | 1,551,266 | `c6b6a9b5bd31a80c3564a516e8800935b89ec0d834c458c7022e9ff166379614` |
| Test evaluation report | `models/intent_eval.json` | 12,998 | `58a7539c38dec76760453df34b81a16b2759c150f7957e56fb0a11c71a4e22f4` |
| Validation metrics | `models/intent_metrics.json` | 8,561 | `405f59e032dbc993d12c9c5e584404d496b27bd8761ea5f23a2d09a32079e31f` |
| Model card | `models/intent_model_card.md` | 822 | `d0b0bbc9e54e4c15b0a4b5c6e6df38511d53e7a3014ae85a8610efb237713aea` |
| Dataset manifest | `training_data/generated/manifest.json` | 7,641 | `f85cb0344c788e7ebf4fcb87d321f0f367d40827133c0a31f70bfe956bff2d5d` |
| Training split (518) | `training_data/generated/train.jsonl` | 56,013 | `bf30ef79bb859a08a304dadd29e15d944f6dbf0310fbfeb3c89b98504431fa02` |
| Validation split (118) | `training_data/generated/validation.jsonl` | 13,307 | `1f4d21707a59f03c5bd623ed40750d1aa67daa5901d7ba625c0e55fb53924b8e` |
| Test split (129) | `training_data/generated/test.jsonl` | 13,969 | `8f54a146390d469b1b5facef71fc3de2ed5962725d2e9f2789640f5d326f0b96` |

The package version is `0.1.0` (`app/personal_assistant/__init__.py`). The model
was trained at `2026-10-27T11:32:41Z` from 518 authored-template examples with 52
labels and 2,688 bag-of-words features. It is a multinomial logistic-regression
classifier trained from scratch with NumPy; `"pretrained": false`. No
user-approved examples were included. The test metrics are:

- raw test accuracy: `0.945736` (122/129);
- validation accuracy: `0.872881` (103/118);
- gate coverage: `0.20155` (26/129);
- accepted accuracy: `1.0`.

The synthetic held-out split measures consistency with authored templates, not
general language understanding. See [MODEL_POLICY.md](MODEL_POLICY.md) and
`models/intent_model_card.md`.

## Python runtime dependencies

These packages are imported by the application. The `RECORD SHA-256` column is
the SHA-256 of the installed `*.dist-info/RECORD` file, which lists the installed
files and their recorded hashes. It identifies this installation, but cannot
prove where a wheel came from.

| Package | Version | License metadata | Role | RECORD SHA-256 |
|---|---:|---|---|---|
| NumPy | 2.5.4 | `BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0` | Arrays, MFCC/DTW, model training/inference, formant DSP | `7e43fc3b65fd1108daf29b7cce11a240c1c5609df0473ce7ec45961fbcee731a` |
| psutil | 7.2.2 | BSD-3-Clause | Processes, battery, network interfaces, process trees | `49d2d7bc9925625eba227207bc11db0fdcd1496fc1e472fdedd6038cc36777d1` |
| PyYAML | 6.0.3 | MIT | Local configuration parsing (`safe_load`) | `1d4eed6ae5daeb36dfa6054cca5fd095c1a534a9b2f894e64d713aa19c78770e` |
| pypdf | 6.21.2 | BSD-3-Clause | Read-only PDF text extraction | `91076ee26673ce395dddf8b115be68d9ccf7b9b7505cec90d25df4022690a249` |
| Pillow | 13.0.0 | MIT-CMU | Screenshot capture and PNG validation | `1ea986d185ec0810a4821d06170cd310a217f9fc13e0145862d56acd3b64415e` |

Declared ranges are in `requirements.txt`; the exact recorded resolution is in
`requirements.lock.txt`. The lock file is a version lock, not a hash-locked
supply-chain manifest.

## Test and build tooling

| Package | Version | License metadata | Role | RECORD SHA-256 |
|---|---:|---|---|---|
| pytest | 9.1.1 | MIT | Automated tests | `1d972653bcc21666a22bb7abdd40842b750e7522d4d55eecc05def61502148af` |
| colorama | 0.4.6 | BSD (classifier; see package `LICENSE.txt`) | pytest terminal output | `805d5dd03822c32441330562f6eec1b1565a4dcf7296c8bc8321e975d4dda85f` |
| iniconfig | 2.3.1 | MIT | pytest configuration | `235647c374b767c5a9e2c3c6862b9ce12bd358c30978b9e043373460bd68f5f1` |
| packaging | 26.3 | `Apache-2.0 OR BSD-2-Clause` | pytest/version parsing | `fb96d1369585ab87d62ad98728c89d2e67274ad060f012c85e358ba17ca92577` |
| pluggy | 1.7.0 | MIT | pytest plugin engine | `e2fe27719bb53484b241e2fec0ee3f24183484c61e1483e6953e74c08e0bf539` |
| Pygments | 2.21.0 | BSD-2-Clause | pytest output highlighting | `e62452ca938809545fee57ca890715bbdbd00aa1889bc1b23966eea2eff5a702` |
| pip | 26.2.2 | MIT | Environment/setup tooling; not imported at runtime | `9835b1fbf90799e592f8dffa17646dfbc72c9c11fcb7c1af9f8e9df1b7c24500` |

`pyinstaller` is **not installed**. `scripts/package.ps1 -SkipTests` reports that
absence and exits with code `2`; no packaged executable was produced.
PyInstaller support for CPython 3.14 was not assumed or verified.

## Operating-system and standard-library components

| Component | Recorded version | Use |
|---|---|---|
| CPython | 3.14.6 | Runtime |
| Tcl/Tk | 8.6.15 | Desktop UI (`tkinter`) |
| SQLite | 3.50.4 | Local memory database (`sqlite3`) |
| WinMM | Windows 11 OS component | Audio capture/playback through `ctypes` |
| Windows Win32 APIs | Windows 11 build 26300 | Windows, clipboard, keyboard, screenshots, display |
| PowerShell | Windows 11 OS component | Setup/launch scripts; optional SAPI helper |

The UI deliberately uses Tkinter rather than PySide6, and audio uses WinMM rather
than sounddevice/PortAudio. Compatible wheels for those packages were not
available for the inspected CPython 3.14 installation. This is an
environment-driven tradeoff; see [ARCHITECTURE.md](ARCHITECTURE.md).

## Deliberately absent

No cloud SDK, LLM runtime, pretrained embedding/acoustic/vision model, telemetry
client, remote server framework, browser automation binary, or automatic updater
is installed or required. The following are specifically absent from the
recorded environment:

- PyInstaller;
- PySide6/PyQt and other downloaded GUI runtimes;
- sounddevice/PortAudio;
- requests/httpx/Flask/FastAPI/websockets;
- Playwright/Selenium or downloaded browser binaries;
- Whisper or any pretrained speech-recognition model.

Windows SAPI voices are OS components and are disabled by default. They are
available only after both `speech.tts_engine: sapi` and
`speech.allow_os_pretrained_tts: true` are set explicitly. There is no automatic
fallback from the from-scratch formant synthesizer to SAPI.

## Provenance summary

| Component | Where it came from | Network at runtime? |
|---|---|---|
| Application source | This repository | No |
| Intent model | Trained locally from `scripts/generate_training_data.py` templates | No |
| Speech templates | None installed; owner must record them locally | No |
| Formant TTS | Rule tables and DSP implemented in this repository | No |
| Python packages | Installed by `setup.ps1` from pip (or a local wheelhouse) | No |
| SAPI voice (optional) | Microsoft component already installed in Windows | Not required by the helper; OS behavior is outside the Python guard |

## Reproduction and verification

To recreate the Python package set:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
```

Then verify the environment and the artifacts:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\diagnose.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\offline_check.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\test.ps1
```

The recorded acceptance results and the manual tests that were **not** performed
are in [ACCEPTANCE.md](ACCEPTANCE.md). The source bundle's `checksums.sha256`
contains hashes for every file in that bundle, including this document.
