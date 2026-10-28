# Recorded acceptance run

- **Build:** source build `0.1.0`
- **Date:** 2026-10-27
- **Environment:** Windows 11 Home `10.0.26300`, CPython `3.14.6`, 16 GB-class
  RAM, no GPU
- **Scope:** the local source tree, installed Python environment, command-line
  diagnostics, safe self-test, automated suite, PowerShell wrappers, source
  bundle, and the honest failure path for the optional packaged build

This is the record of what was actually run. It is not a claim that untested
manual interactions, a packaged executable, or general speech recognition were
validated. A later voice-integration run is recorded in the
[addendum](#voice-integration-addendum--2026-10-28); the table below is the
original 2026-10-27 acceptance run.

## Result summary

| Area | Result | Evidence |
|---|---|---|
| Automated tests | **PASS** | 110 passed, 1 skipped |
| Built-in self-test | **PASS** | 15/15 checks |
| Formant TTS code path | **PASS** | NameError found during install verification and fixed; regression test and direct `SPEAK_OK` probe |
| Diagnostics | **PASS with 1 warning** | Healthy; 0 errors; missing user voice templates |
| Offline guard check | **PASS for the Python process** | Strict mode; external blocked; loopback works |
| Setup script | **PASS** | Existing venv reused; runtime requirements satisfied; compile check passed |
| Launch/CLI wrappers | **PASS** | Version and one-shot status command returned real local state |
| PowerShell syntax | **PASS** | All 7 scripts parsed with no errors |
| Source bundle | **PASS** | Zip created; internal checksum verification passed; personal paths absent |
| PyInstaller package | **NOT BUILT (honest exit 2)** | PyInstaller absent; script reports it and exits 2 |
| Real microphone/ASR | **NOT RUN** | Requires explicit consent and owner recordings |
| Perceptual TTS test | **NOT RUN** | Requires a human listener |
| Real app launch/close | **NOT RUN** | Requires explicit consent; no application was started |
| Clipboard/screenshot changes | **NOT RUN** | Requires explicit consent; automated tests avoid side effects |

## Voice integration addendum — 2026-10-28

This addendum records the optional local Vosk and faster-whisper speech
integration and the opt-in “hearty” wake-word listener. The work described here
is part of the `main` branch history.

| Area | Result | Evidence |
|---|---|---|
| Vosk runtime | **PASS** | `vosk 0.3.45` installed on CPython 3.14.6; `cffi 2.1.1` |
| Vosk model installer | **PASS** | Network download of `vosk-model-small-en-us-0.15` succeeded; archive SHA-256 `30f26242…42498`; safe extraction and `--check` succeeded |
| Whisper runtime | **PASS** | `faster-whisper 1.2.1`, `ctranslate2 4.8.4`, local `faster-whisper-tiny.en` (78,090,594 bytes, 4 files) |
| Whisper model loader | **PASS** | Loaded from `models/whisper` with `local_files_only=True`; worker preload 0.95–1.36 s; repeated decode 0.50–0.67 s |
| Push-to-talk Whisper path | **PASS** | `SpeechService.record_and_transcribe` with captured PCM returned `create a file test.txt` (confidence 0.82) in 1.73 s including worker startup |
| Vosk + Whisper coexistence | **PASS** | Persistent Whisper worker with `CT2_USE_MKL=0` survived Vosk model load and decoded `create a file test.txt` |
| Clean-audio recognition | **PASS** | SAPI-generated WAV transcribed as `hardy open notepad`; wake alias matched |
| Wake-state dispatch | **PASS (synthetic stream)** | Fake PCM stream produced `listening → awake → muted` and command `open notepad` at confidence `1.0` |
| End-to-end wake + Whisper | **PASS (synthetic WAV)** | `tts_hybrid.wav` produced `listening → loading → awake → muted` and dispatched `create a file test.txt` (confidence 0.77) |
| Wake grammar on live capture | **PASS** | Constrained Vosk found all four spoken “hearty” test phrases in `wake_live_probe2.wav`; open-vocabulary Vosk was too slow on this CPU |
| Wake GUI auto-start | **PASS** | With the installed config, the withdrawn Tk window reported `wake: listening`, a running listener, and the local model available |
| Real microphone startup | **PASS** | A real WinMM stream opened with the installed config; status moved `loading → listening` with no microphone, Vosk, or Whisper-worker errors, then stopped cleanly |
| Automated tests | **PASS** | `136 passed, 1 skipped`; voice-text, Vosk (including shared-model loading), Whisper worker, wake-listener (fake-stream dispatch, mid-session worker failure, stop-before-deadline, VAD capture), config, UI-badge, and voice-command routing checks |
| Self-test | **PASS** | `15/15` |
| Diagnostics | **PASS, no warnings** | Vosk available; wake word enabled; 53 tools; offline guard installed |
| Real microphone acoustic validation | **NOT VALIDATED** | Two captures (RMS ~0.16-0.17) were dominated by unrelated ambient audio; the model returned unrelated text |

The automated suite still never opens a microphone, plays audio, or downloads a
model. The wake dispatch check used an in-memory fake stream; the end-to-end check
used the synthetic `tts_hybrid.wav`; model and installer checks used local files.
The real-microphone limitation is a test environment issue, not a reported
success: a quiet room or headset is required to validate acoustic wake-word
triggering.

Whisper runs in a persistent local child process because loading Vosk and
CTranslate2 in one process intermittently failed with
`mkl_malloc: failed to allocate memory` on this machine. The worker sets
`CT2_USE_MKL=0`, `OPENBLAS_NUM_THREADS=1`, and `OMP_NUM_THREADS=1`; no audio is
written to disk and only raw PCM crosses the local pipe. If the worker cannot
start, the listener records the reason and falls back to the local Vosk
recognizer.

While re-running the suite, the typing-risk check was also hardened for a NULL
foreground-window handle (a locked or non-interactive desktop). It now keeps
typing at sensitive risk instead of raising a `TypeError`; this is not a voice
feature, but it was needed for a clean suite run in this session.

## Automated test suite

Command, run through the project wrapper:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\test.ps1
```

Recorded output:

```text
.................s...................................................... [ 64%]
.......................................                                  [100%]
=========================== short test summary info ===========================
SKIPPED [1] tests\test_config_paths.py:132: Creating symbolic links is not permitted in this Windows session.
110 passed, 1 skipped in 39.23s
```

The same suite run directly with
`.venv\Scripts\python.exe -m pytest tests -q` also passed with 110 passed and 1
skipped (36.03s). The skip is an environment limitation of this Windows session,
not a disabled security test: Windows did not permit creating a symbolic link for
the traversal test. The path policy still resolves links defensively.

The tests use isolated temporary `PA_HOME` roots and do not use a real microphone,
play audio, alter the clipboard, launch applications, or capture screenshots
without explicit consent. The Tkinter test creates a window and immediately
withdraws/closes it.

### Covered test areas

The 111 collected tests include configuration/path policy, offline blocking,
JSON-schema validation, permission/approval behavior, kill switch, cancellation,
timeouts, task state transitions, memory storage/export/retention/disable,
filesystem operations and undo, ZIP safety (zip-slip, entry count, expansion),
document extraction, Windows read-only status tools, app/typing risk handling,
browser URL policy, speech policy/device reporting, CLI subprocesses, recovery
journal behavior, and a minimal Tk window lifecycle.

### Post-install TTS correction

During installation verification, the default formant speech output failed with
`NameError: name 'phone' is not defined` in `speech/tts.py`. The code referenced
`phone` instead of the computed `phones` value. This was a real functional defect,
not a display issue: every spoken response would have been silent. It was fixed,
a no-playback regression test was added to `tests/test_system_voice.py`, and a
direct synthesis/playback probe then returned `SPEAK_OK`. The full suite was
re-run with 110 passed and 1 skipped. Perceptual quality remains unvalidated;
only the code path and local playback call were exercised.

## Built-in self-test

Command:

```powershell
.\.venv\Scripts\python.exe -m personal_assistant --self-test
```

Result:

```text
Self-test: 15/15 passed
```

The passing checks were:

1. `write+verify`
2. `read+verify`
3. `copy+verify`
4. `rename+verify`
5. `quarantine-delete`
6. `undo`
7. `memory-write`
8. `memory-read`
9. `status`
10. `unknown-command-safe`
11. `approval-bridge`
12. `kill-switch`
13. `kill-switch-activate`
14. `kill-switch-blocks`
15. `kill-switch-reset`

The self-test used a temporary sandbox and reported verified file results and a
working undo record. It did not touch the real microphone, clipboard, desktop
applications, or screenshots.

## Diagnostics

Command:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\diagnose.ps1
```

Recorded output:

```text
Local Personal Assistant 0.1.0 diagnostics
Config: built-in defaults
Root:   C:\my personal assistant
  [OK  ] platform: Windows 11 10.0.26300
  [OK  ] python: 3.14.6 at C:\my personal assistant\.venv\Scripts\python.exe
  [OK  ] dependency:numpy: 2.5.4
  [OK  ] dependency:psutil: 7.2.2
  [OK  ] dependency:PyYAML: 6.0.3
  [OK  ] dependency:pypdf: 6.21.2
  [OK  ] dependency:Pillow: 13.0.0
  [OK  ] tkinter: Tk 8.6
  [OK  ] directories: 12 runtime directories ensured under C:\my personal assistant
  [OK  ] sandbox-write: C:\my personal assistant\sandbox
  [OK  ] package-data:memory-schema: C:\my personal assistant\app\personal_assistant\memory\schema.sql
  [OK  ] package-data:sapi-script: C:\my personal assistant\app\personal_assistant\speech\sapi_tts.ps1
  [OK  ] audio-devices: inputs=1, outputs=1
  [OK  ] intent-model: C:\my personal assistant\models\intent_model.json
  [WARN] voice-templates: No user voice templates are installed.
  [OK  ] tool-registry: 53 tools registered
  [OK  ] offline-mode: strict (guard installed: True)
  [OK  ] loopback-sockets: loopback sockets work

Result: healthy (0 error(s), 1 warning(s))
```

The warning is expected and honest: no owner voice templates were recorded, so
closed-vocabulary speech input has no phrases to recognize yet. The microphone
device itself was detected. No microphone stream was opened by diagnostics.

## Offline guard check

Command:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\offline_check.ps1
```

Recorded output:

```text
Offline guard check
  Mode:              strict
  Guard installed:   True
  External blocked:  True
  Detail:            Blocked non-loopback network access during create_connection: ('192.0.2.1', 9). Strict offline mode is enforced.
  Loopback sockets:  True (loopback sockets work)
  Application-level guard: blocks Python socket traffic to non-loopback addresses. It does not control child processes, native libraries, or other applications. Use optional OS-level firewall rules for OS-level enforcement; see docs/SECURITY.md.
```

This proves the in-process Python guard blocks a non-loopback connection and
permits loopback. It does **not** prove that child processes, native libraries,
browsers, or the operating system are offline. No OS firewall rule was created or
claimed.

## PowerShell and launcher acceptance

All scripts were parsed with
`System.Management.Automation.Language.Parser.ParseFile`:

```text
OK   bundle.ps1
OK   diagnose.ps1
OK   launch.ps1
OK   offline_check.ps1
OK   package.ps1
OK   setup.ps1
OK   test.ps1
```

`launch.ps1 --version`:

```text
personal-assistant 0.1.0
```

`launch.ps1 --command "status"` returned live local state:

```text
Offline mode: strict
Kill switch: off
Tools registered: 53
Memory: enabled (0 tasks, 0 preferences)
Intent model: loaded - label suggestion only
Speech input available: True; speech output available: True (formant)
Undo records: 0 (0 pending)
Configuration: built-in defaults
```

The original console output used an em dash, which the legacy console code page
displayed as `?`/replacement text; the underlying status values were correct.
This is a terminal encoding display issue, not evidence of a different action.

`setup.ps1 -NoConfig` reused the existing `.venv`, upgraded pip from `26.1.2` to
`26.2.2`, confirmed all five runtime requirements were already satisfied, and
completed the compile check. It did not create or overwrite a local configuration
file. The optional `-Dev` path was not run because it would install PyInstaller,
which is intentionally absent from this recorded environment.

## Source bundle

Command:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bundle.ps1
```

The script created `dist/LocalPersonalAssistant-0.1.0-portable.zip`. The archive
was expanded to a temporary directory and every entry in `checksums.sha256` was
recomputed with SHA-256: **0 mismatches**. The final bundle was inspected for
excluded paths:

- `.venv` — absent
- `data` — absent
- `logs` — absent
- `sandbox` — absent
- `config/config.yaml` — absent
- user voice templates — absent

The bundle includes the application, docs, tests, scripts, `config.example.yaml`,
the from-scratch model and metrics, and the synthetic training splits. It excludes
personal runtime state and recordings. The checksum file is an integrity aid, not
a signature.

## Optional packaged build

Command:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\package.ps1 -SkipTests
```

Recorded output:

```text
PyInstaller is not installed in this environment.
Install it explicitly with:
  .venv\Scripts\python.exe -m pip install "pyinstaller>=6.15,<7"
PyInstaller support for CPython 3.14 is not assumed; verify before relying on this build.
```

Exit code: **2**. No `dist\LocalPersonalAssistant` executable exists. This is the
intended honest failure path; a packaged build must not be claimed until
PyInstaller is installed on a compatible interpreter and the build actually
succeeds.

During the first acceptance attempt, the missing PyInstaller import produced a
PowerShell terminating error and exit code 1. The script was corrected to capture
the optional import failure without treating it as fatal, then re-run; the final
result above is from the corrected script.

## Manual and environmental items not validated

These items were deliberately not performed, and no result is claimed for them:

1. **Real microphone capture and closed-vocabulary ASR.** Requires explicit user
   consent, a quiet recording session, and owner recordings. Only device
   enumeration and policy status were tested.
2. **Perceptual text-to-speech quality.** The formant synthesizer was exercised
   through policy/status tests, but no human listened to it during acceptance. It
   is documented as robotic and unvalidated.
3. **Launching and closing a real application (for example Notepad).** Not run
   because it would change the interactive desktop and requires explicit consent.
   App discovery, risk classification, and safe misses were tested.
4. **Clipboard mutation and full-screen screenshot capture.** Not run to avoid
   changing the user's clipboard or capturing personal screen content. Their
   policy, validation, and failure paths were tested where possible.
5. **Interactive GUI use.** The Tk window construction/close lifecycle was tested;
   a human usability session was not.
6. **Symlink traversal.** The test was skipped because this Windows session could
   not create symlinks. Defensive link resolution remains in the path policy.
7. **PyInstaller executable.** Not built; see above.
8. **Downloaded wake-word or general ASR/TTS models.** Superseded for the
   explicitly installed local Vosk and Whisper models by the 2026-10-28 addendum
   above. Real acoustic validation remains unvalidated; no cloud speech model is
   installed.
9. **OS-level network isolation.** Not configured; the Python guard boundary is
   documented explicitly.

## Acceptance disposition

The source build is accepted for its documented local functionality **with the
gaps above**. It is a complete, runnable reference implementation, not a
production-hardened security product and not evidence of general language
understanding. The deterministic parser remains the primary command path; the
intent classifier is a low-coverage fallback that can only suggest a label.

The recorded model metrics, package hashes, and artifact provenance are in
[INSTALLED_COMPONENTS.md](INSTALLED_COMPONENTS.md). The security boundary, model
policy, and capability matrix are in [SECURITY.md](SECURITY.md),
[MODEL_POLICY.md](MODEL_POLICY.md), and [CAPABILITIES.md](CAPABILITIES.md).
