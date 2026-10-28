# Dependencies and environment

## Recorded environment

| Item | Value |
|---|---|
| Operating system | Windows 11 Home, version 10.0.26300 (64-bit) |
| CPU | Intel Core 7 150U, 10 cores / 12 logical processors |
| RAM | 15.7 GiB usable (16 GB class) |
| GPU | None used; no CUDA, DirectML, or GPU acceleration path |
| Python | CPython 3.14.6, 64-bit, MSC v.1944 |
| Virtual environment | `.venv` in the project root |
| Package manager | pip |
| GUI toolkit | Tkinter 8.6 from the Python standard library |
| Audio API | WinMM through `ctypes` from the standard library |

The project metadata supports CPython **3.11-3.14** (`>=3.11,<3.15`). Only
3.14.6 was exercised in the recorded environment. A different minor version is
plausible but not a tested claim; run `--diagnose` and the test suite there.

## Runtime dependencies

These are declared in `requirements.txt` and pinned exactly in
`requirements.lock.txt` for the recorded environment.

| Package | Recorded version | Purpose | Why it is needed |
|---|---:|---|---|
| `numpy` | 2.5.4 | Numeric arrays, MFCC features, softmax model training/inference, formant DSP | Local model and audio math; not a pretrained model |
| `psutil` | 7.2.2 | Processes, battery, network interfaces, process-tree termination | Windows status and safe developer timeout/cancel |
| `PyYAML` | 6.0.3 | Configuration parsing | Local YAML only; `safe_load` is used |
| `pypdf` | 6.21.2 | PDF text extraction | Read-only document support |
| `Pillow` | 13.0.0 | Screenshot capture, PNG validation | Local screenshot support |
| `vosk` | 0.3.45 | Optional offline wake-word detection and fallback command recognition | Apache-2.0 local Kaldi runtime; model is a separate one-time download |
| `faster-whisper` | 1.2.1 | Optional offline general command recognition after the wake word | MIT wrapper around OpenAI's MIT Whisper model through Apache-2.0 CTranslate2; model and worker run locally |

Vosk installs transitive packages (`cffi`, `requests`, `srt`, `tqdm`,
`websockets`, and their dependencies) on this interpreter. The application uses
Vosk's local recognizer only; it does not start a server or use those packages for
cloud calls. Vosk models are **not** Python packages: install
`vosk-model-small-en-us-0.15` separately with
`scripts/install_voice_model.py`.

faster-whisper adds `ctranslate2`, `av`, `huggingface_hub`, `tokenizers`,
`onnxruntime`, and their transitive set. The running assistant imports the local
model from `models/whisper` with `local_files_only=True`; it never downloads
weights. `ctranslate2` runs in a persistent local child process so its native
runtime stays isolated from Vosk. On constrained Windows systems it is configured
with `CT2_USE_MKL=0`; see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

The intended dependency set is deliberately small. The UI uses Tkinter (stdlib)
and audio uses WinMM (stdlib `ctypes`) rather than PySide6 and sounddevice. On
the inspected CPython 3.14 installation, compatible PySide6 and
sounddevice/cffi wheels were not available. This is an environment-driven
tradeoff recorded in [ARCHITECTURE.md](ARCHITECTURE.md), not a claim that those
libraries are inherently unsuitable.

## Development and build dependencies

`requirements-dev.txt` adds:

| Package | Recorded version | Purpose |
|---|---:|---|
| `pytest` | 9.1.1 | Automated tests |
| `pyinstaller` | **not installed** | Optional packaged executables |

The lock file also records pytest's small transitive set that was present:
`colorama 0.4.6`, `iniconfig 2.3.1`, `packaging 26.3`, `pluggy 1.7.0`, and
`Pygments 2.21.0`. These are test-tool dependencies, not runtime imports.

PyInstaller was **not installed** in the recorded environment. Python 3.14
support was not assumed or verified. `scripts/package.ps1` therefore reports the
absence and exits with code 2 rather than fabricating a build. Do not claim a
packaged executable exists unless that run has actually succeeded. If you
install a compatible PyInstaller, the provided spec builds
`LocalPersonalAssistant.exe` and `LocalPersonalAssistantCLI.exe` in a one-folder
layout.

## Standard-library components

Notable stdlib use:

- `tkinter` — desktop window and dialogs;
- `ctypes` / `ctypes.wintypes` — WinMM audio, Win32 windows, clipboard, keyboard,
  display APIs;
- `sqlite3` — local memory database;
- `zipfile` — archive creation/extraction with explicit safety checks;
- `json`, `csv`, `hashlib`, `pathlib`, `subprocess`, `threading`,
  `concurrent.futures`, `logging`, `argparse`, `tempfile`, `shutil`;
- `winreg` — read-only startup entries (Windows only);
- `socket` — local loopback tests and the in-process guard.

Tkinter and WinMM are part of the Windows Python distribution/OS and are not pip
dependencies. WinMM is not a Python package; it is the OS multimedia API.

## Deliberately absent

The repository does **not** depend on or bundle:

- PySide6, PyQt, or a downloaded GUI runtime;
- sounddevice/PortAudio or a downloaded audio library;
- a remote server framework (Flask, FastAPI, or similar); `requests` and
  `websockets` are present only as transitive Vosk dependencies;
- an LLM SDK, embeddings package, or TTS cloud SDK;
- an automatic updater, telemetry client, or runtime model downloader;
- Playwright/Selenium browser binaries or a downloaded browser;
- PyInstaller in the recorded environment.

The Android bridge is not implemented and has no server dependency. See
[ANDROID_INTEGRATION.md](ANDROID_INTEGRATION.md).

## Installation

### Normal setup

From the project root:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -Dev
```

Setup creates/uses `.venv`, installs requirements, copies
`config/config.example.yaml` to `config/config.yaml` unless `-NoConfig` is
given, creates the runtime directories, and runs a compile check. `-Dev` adds
pytest/PyInstaller; without it, only runtime requirements are installed.

The optional speech models are separate one-time downloads, never fetched by the
running assistant:

```powershell
.\.venv\Scripts\python.exe .\scripts\install_voice_model.py
.\.venv\Scripts\python.exe .\scripts\install_voice_model.py --check
.\.venv\Scripts\python.exe .\scripts\install_whisper_model.py
.\.venv\Scripts\python.exe .\scripts\install_whisper_model.py --check
```

Then set `speech.asr_engine` to `vosk` or `whisper`. Wake detection always uses
Vosk; `speech.wake_command_engine: whisper` (the installed default) uses the local
Whisper worker for more accurate wake commands. For hands-free use, also set
`speech.wake_word_enabled: true` and use a microphone you are comfortable leaving
open while the listener is active.

### Offline wheelhouse

On a connected machine with the same Python version and architecture:

```powershell
python -m pip download -r requirements-dev.txt -d .\wheels
```

Copy the `wheels` folder to the target and run:

```powershell
.\scripts\setup.ps1 -Offline -Wheelhouse .\wheels -Dev
```

`-Offline` uses `pip --no-index --find-links` only. If the wheelhouse is missing
or lacks a compatible wheel, setup fails; it does not silently use the network.
Wheels are platform/version-specific. Build the wheelhouse for Windows and the
target CPython minor version.

### Exact reproduction

The recorded environment can be recreated with:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
```

The lock file is an exact set, but it is not a hash-locked supply-chain manifest.
For a high-assurance install, add hashes and verify the wheel source.

## Verification

```powershell
.\scripts\diagnose.ps1
.\scripts\diagnose.ps1 -Json
.\scripts\test.ps1
```

Diagnostics check the platform, Python, each package's importability/version,
Tkinter, runtime directories, sandbox write, packaged data files, audio devices,
the intent model, the configured speech engines (templates, Vosk, and/or
Whisper), wake-word configuration, the tool registry, offline guard installation,
and loopback sockets. A missing optional model or voice templates is a warning; a
missing runtime dependency is an error.

## Updating dependencies

There is no runtime updater. To update deliberately:

1. review the new versions and their licenses/advisories;
2. update `requirements.txt` bounds and, if appropriate, the lock file;
3. run setup in a fresh `.venv`;
4. run the full test suite, `--self-test`, `--diagnose`, and `--offline-check`;
5. re-run the model pipeline only if the numeric stack changed in a way that
   affects reproducibility;
6. update [INSTALLED_COMPONENTS.md](INSTALLED_COMPONENTS.md) and the recorded
   acceptance environment.

Do not let a package install trigger a model download or a new network service.
The runtime must remain local-first.

## Licensing note

The project itself is MIT (see `LICENSE`). Third-party packages retain their own
licenses; the recorded metadata reports NumPy under a BSD-3-Clause/0BSD/MIT/
Zlib/CC0 combination, psutil and pypdf under BSD-3-Clause, PyYAML under MIT,
Pillow under MIT-CMU, pytest/pluggy/iniconfig under MIT, packaging under
Apache-2.0 or BSD-2-Clause, Pygments under BSD-2-Clause, colorama under its own
license, Vosk/the small English model under Apache-2.0, faster-whisper and the
Whisper model under MIT, and CTranslate2/ONNX Runtime/`av` under their respective
upstream licenses. Consult the installed package metadata and upstream texts
before redistributing a frozen build; this summary is not legal advice.
