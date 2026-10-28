# Local Personal Assistant

A privacy-first, offline-first personal assistant for Windows 11. It accepts
typed commands (and opt-in push-to-talk speech), performs real local actions
through a permissioned tool registry, verifies what it can, records an audit
trail, and keeps memory in a local SQLite database.

**Status:** source build `0.1.0`, exercised on Windows 11 (build 26300) with
CPython 3.14.6. This is a complete local implementation, not a production-
hardened security product. It has no cloud AI, no telemetry, no silent online
fallback, and no remote control server.

Source: <https://github.com/nagakarthikey2602/my-personal-assistant>

## What works

- **Files and documents:** write/read/search/list, create folders, copy, move,
  rename, quarantine-delete, ZIP create/extract, duplicate detection, inventory,
  and undo of supported filesystem operations. Reads `.txt`, `.md`, `.json`,
  `.csv`, `.log`, `.py`, `.pdf`, and `.docx`; writes text and `.docx`.
- **Applications and windows:** list installed Start Menu/registered apps,
  launch known apps and observe process/window evidence, list visible windows,
  focus/close an unambiguous app, and send Unicode text to a safe foreground
  editor. No accessibility-tree verification is implemented.
- **Windows status:** display, network/Wi-Fi, Bluetooth service, power plan and
  battery, startup registry entries, Windows Settings pages, and File Explorer
  folders. It does not change OS settings or startup entries.
- **System and desktop:** hardware/runtime info, top processes, battery, audio
  devices, offline guard status, media keys, clipboard read/write, and a
  full-screen PNG screenshot into an allowed write root.
- **Developer tools:** six exact allowlisted commands (`pytest`, `unittest`,
  `compileall`, three Git read commands) in a writable project folder, plus
  deterministic log failure scanning. No arbitrary shell or command arguments.
- **Browser:** opens local `file://` pages; external URLs only after leaving
  strict offline mode and explicitly enabling `tools.browser_network_enabled`.
  No form filling or page automation. Page text is untrusted data.
- **Memory:** explicit preferences, task history, training examples, export,
  retention, disable switch, and delete operations in local SQLite.
- **Voice:** WinMM devices, push-to-talk capture with energy VAD, optional local
  Vosk and Whisper models for general English commands, an opt-in continuous
  wake-word listener (default “hearty”), and a from-scratch formant synthesizer.
  The original user-recorded MFCC/DTW recognizer remains available. Optional SAPI
  voices are policy-gated and off by default.
- **Safety:** four risk levels, approval prompts, strict path allowlists,
  protected internal data, sensitive-name blocking, kill switch, cancellation,
  timeouts, quarantine instead of permanent delete, recovery journal, bounded
  audit log, and structured results that distinguish verified from unverified.
- **Interfaces:** Tkinter desktop window, interactive console, one-shot command,
  diagnostics, offline check, self-test, JSON output, and tool catalog listing.

## Requirements

- Windows 11. The code uses Win32 APIs for windows, audio, clipboard, and
  screenshots; it is not a cross-platform application.
- CPython **3.11-3.14** with Tkinter. This build used **3.14.6**.
- 16 GB RAM was available during development; no GPU is used.
- The desktop UI uses Tkinter from the standard library because PySide6 had no
  compatible wheel for the inspected Python 3.14 installation. Audio capture and
  playback use WinMM through `ctypes`, not `sounddevice`.
- No cloud API, API key, or telemetry endpoint is needed or supported. General
  speech recognition is optional and local: Vosk/Kaldi handles wake detection and
  fallback recognition, and faster-whisper handles general wake commands. Both
  models are downloaded once by the owner (about 40 MB and 75 MB), never at
  runtime.

## Quick start

From the repository root in PowerShell:

```powershell
# Optional: review config/config.example.yaml first.
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -Dev

# Desktop GUI
powershell -ExecutionPolicy Bypass -File .\scripts\launch.ps1

# Text console
powershell -ExecutionPolicy Bypass -File .\scripts\launch.ps1 --cli

# One command, then exit
powershell -ExecutionPolicy Bypass -File .\scripts\launch.ps1 --command "status"

# Diagnostics and safe self-test
powershell -ExecutionPolicy Bypass -File .\scripts\diagnose.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\diagnose.ps1 -SelfTest
powershell -ExecutionPolicy Bypass -File .\scripts\offline_check.ps1
```

Setup is the only script that may use the network (pip downloads). To install
without network access, prepare a local wheelhouse and use:

```powershell
.\scripts\setup.ps1 -Offline -Wheelhouse .\wheels -Dev
```

The assistant itself never downloads models, updates, or content at runtime.

If the environment is already prepared, the direct form is:

```powershell
$env:PYTHONPATH = "$PWD\app"
.\.venv\Scripts\python.exe -m personal_assistant --cli
```

## Example commands

The parser is a deterministic, closed grammar. It asks for clarification rather
than guessing when required arguments are missing.

```text
create a file notes.txt in sandbox with content Hello local world
read notes.txt
list sandbox
search sandbox for hello
copy notes.txt to notes-copy.txt
rename notes-copy.txt to notes-final.txt
delete notes-final.txt
undo
```

```text
remember that my editor is notepad
what do you remember
export memory to memory.json
forget editor
disable memory
enable memory
```

```text
status
capabilities
help
open notepad
take a screenshot
what is my battery status
list my windows
kill switch
resume
```

Use quotes for text containing punctuation:

```text
type "Hello, world!" in notepad
create a file "meeting notes.md" in sandbox with content "Agenda: local only"
```

## Permission model

| Risk | Label | Default behavior |
|---|---|---|
| 1 | read-only | automatic |
| 2 | reversible | automatic, but may require approval if configured |
| 3 | sensitive | explicit approval by default |
| 4 | privileged/high-impact | explicit approval by default; force-kill is level 4 |

`permissions.allowed_tools` and `permissions.denied_tools` provide a second
allow/deny layer before risk approval. The executor validates the strict tool
schema, checks the kill switch and network policy, requests approval, runs the
tool, records audit events, and returns a structured result.

Destructive file deletion is a quarantine operation. Supported filesystem
changes are journaled and can be undone with `undo`. App/browser/typing actions
are **not** covered by the filesystem undo journal.

## Paths and memory

The default writable root is only `<APP_ROOT>\sandbox`. `data`, `logs`, `config`,
`models`, and `training_data` are protected internal locations and are blocked
from the general file tools. Read roots default to the app root plus the user's
Downloads, Documents, and Desktop folders. Sensitive names such as `.env`, private
keys, credential stores, and Windows system directories are denied.

The SQLite database stores explicit preferences, task history, and
user-approved training examples. It is local and unencrypted; rely on Windows
account permissions and disk encryption if needed. Use the Memory inspector or
commands to inspect, export, disable, or delete records. `store_outputs` is off
by default; command history may still contain personal text.

## Voice input and output

Voice input has two local modes. The **template** mode is a closed-vocabulary
recognizer using MFCC features and DTW against phrases you record:

```powershell
.\.venv\Scripts\python.exe .\scripts\record_voice_templates.py
```

The **Vosk** mode recognizes general English commands with a pretrained Kaldi
model that runs entirely on this PC. Install the model once; the application
never downloads it at runtime:

```powershell
.\.venv\Scripts\python.exe .\scripts\install_voice_model.py
```

For wake commands, install the optional local Whisper model once as well:

```powershell
.\.venv\Scripts\python.exe .\scripts\install_whisper_model.py
```

Set `speech.asr_engine` to `vosk` or `whisper` in `config/config.yaml`. The
installed configuration uses `vosk` for general/push-to-talk recognition and
`wake_command_engine: whisper` for more accurate hands-free commands; wake
detection itself is grammar-constrained Vosk. Use `Push to talk` in the window or
`--voice` in the console. For hands-free operation, click `🎧 Wake` or run
`--wake`; the microphone stays open while the listener is active, the window
shows its state, and `Wake: off` releases it. “Hearty” is the wake word; the
small model often spells it “hardy”, so both are accepted, along with “harty” and
“heart y”.

Whisper runs in a persistent local child process. That isolates CTranslate2 from
the Vosk runtime and keeps both engines reliable on Windows systems with a small
commit limit; `CT2_USE_MKL=0` avoids the native MKL allocation failure. The model
is loaded with `local_files_only=True`. There is no cloud fallback, no telemetry,
and no audio written to disk by the listener. See `docs/MODEL_POLICY.md` and
`docs/SECURITY.md` for the model provenance and privacy boundary.

The default speech output is a deterministic formant synthesizer implemented in
this repository. It sounds robotic and has not been perceptually validated.
Microsoft SAPI voices can be enabled only by editing configuration and setting
`speech.allow_os_pretrained_tts: true`; there is no automatic fallback to SAPI.

## Local intent model

`models/intent_model.json` is a small multinomial logistic-regression classifier
trained from scratch with NumPy on generated, reviewed command templates. It is
used only when deterministic rules cannot parse a command, and only to suggest an
intent label. It never supplies tool arguments and never bypasses policy.

Recorded test split (129 examples): **94.57% raw accuracy** (122/129). Validation
split (118 examples): **87.29%**. With the strict confidence/margin gate, only
**20.16%** of test examples are accepted, at **100% accepted accuracy**. This
synthetic split measures consistency with authored templates, not general
language understanding.

Retrain:

```powershell
.\.venv\Scripts\python.exe .\scripts\generate_training_data.py
.\.venv\Scripts\python.exe .\scripts\train_intent_model.py
.\.venv\Scripts\python.exe .\scripts\evaluate_intent_model.py
```

See `docs/CAPABILITIES.md` for the full capability matrix and limitations.

## Offline guarantee and its boundary

With `app.offline_mode: strict` (the default), the application installs an
in-process guard that blocks non-loopback Python socket connections, DNS lookups,
and non-loopback binds. `--offline-check` verifies this and tests loopback.
External browser navigation is disabled unless explicitly enabled outside strict
mode.

**This guard is not an OS firewall.** It does not cover child processes, native
libraries, other applications, or traffic generated outside the Python process.
See `docs/SECURITY.md` for the exact boundary and optional OS-level guidance.
The browser tool has no network isolation guarantee; if that matters, leave
strict mode and `browser_network_enabled` at their defaults.

## Tests and measured status

The automated suite is intentionally local and side-effect limited. It uses
temporary `PA_HOME` roots; it does not use a real microphone, play audio, change
the clipboard, launch applications, or capture screenshots without explicit
consent. UI tests create a window and immediately withdraw it.

Measured on 2026-10-28:

```text
pytest tests -q
136 passed, 1 skipped
```

The skip is symlink creation, which this Windows session did not permit; path
policy still resolves links defensively. `--self-test` passed 15/15. Diagnostics
passed every check with 0 errors and 0 warnings using the installed Vosk model
and enabled wake-word configuration. The synthetic wake-listener run detected
“hearty”, captured the command, and dispatched `create a file test.txt` through
the local Whisper worker. The GUI smoke test auto-started the wake listener and
reported `wake: listening`. Real-microphone acoustic validation remains
outstanding because ambient audio dominated the test captures; see
`docs/ACCEPTANCE.md` for the detailed addendum.

## Packaging

- `scripts/bundle.ps1` builds a portable source bundle with docs, tests, the
  from-scratch model, and synthetic training data, excluding personal runtime
  data, voice recordings, and the optional Vosk/Whisper models. Both model
  installers are included; run them after setup if you want local general speech.
- `scripts/package.ps1` builds optional PyInstaller executables. PyInstaller was
  **not installed** in the recorded environment, and Python 3.14 support was not
  assumed or verified. The script reports that honestly and exits with code 2
  instead of pretending a package exists. The spec and build script are provided
  for a compatible environment.

## Repository layout

```text
app/personal_assistant/   application package (core, tools, speech, memory, UI)
config/                   reviewed example; local config.yaml is ignored
scripts/                  setup, launch, diagnose, offline check, tests, bundle, package,
                          voice-template recording and Vosk/Whisper model installers
training_data/generated/  synthetic intent-training splits and manifest
models/                   intent model/metrics plus locally installed Vosk/Whisper models (ignored)
tests/                    unit, integration, security, offline, recovery, UI tests
docs/                     architecture, security, policy, capability matrix, acceptance
packaging/                optional PyInstaller spec and entry point
sandbox/                  default writable area for file actions
data/, logs/              local runtime state (ignored by source bundles)
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Security](docs/SECURITY.md)
- [Model policy](docs/MODEL_POLICY.md)
- [Capability matrix and limitations](docs/CAPABILITIES.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Roadmap](docs/ROADMAP.md)
- [Android integration architecture (future only)](docs/ANDROID_INTEGRATION.md)
- [Dependencies and environment](docs/DEPENDENCIES.md)
- [Installed components and provenance](docs/INSTALLED_COMPONENTS.md)
- [Recorded acceptance run](docs/ACCEPTANCE.md)

## License

MIT. See `LICENSE`.
