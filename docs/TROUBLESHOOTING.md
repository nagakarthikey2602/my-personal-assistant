# Troubleshooting

Start with the local checks before changing code:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\diagnose.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\diagnose.ps1 -Json
powershell -ExecutionPolicy Bypass -File .\scripts\diagnose.ps1 -SelfTest
powershell -ExecutionPolicy Bypass -File .\scripts\offline_check.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\test.ps1
```

The application writes logs under `logs/` and audit records to
`logs/audit.jsonl`. Memory, recovery state, quarantine, and the model live under
`data/` and `models/` in a source build. A frozen build uses
`%LOCALAPPDATA%\PersonalAssistant` unless `PA_HOME` overrides it.

## Setup and interpreter

### `Python was not found on PATH`

Install CPython 3.11-3.14 (64-bit) with Tkinter. Then either add it to `PATH` or
pass it explicitly:

```powershell
.\scripts\setup.ps1 -Python "C:\Path\To\python.exe"
```

The recorded environment used CPython 3.14.6. Do not use a 32-bit interpreter
for the normal 64-bit Windows setup.

### The `.venv` is missing or broken

```powershell
Remove-Item -LiteralPath .\.venv -Recurse -Force
.\scripts\setup.ps1 -Dev
```

This is destructive only to the local virtual environment. Setup may use pip
network access. On an isolated machine, prepare a wheelhouse and use:

```powershell
.\scripts\setup.ps1 -Offline -Wheelhouse .\wheels -Dev
```

If the wheelhouse is missing, the script fails honestly instead of contacting
the network.

### `pip` cannot reach the network during setup

Setup is the only supplied step that may download packages. Use a wheelhouse
built on a compatible machine:

```powershell
# On a connected machine with the same Python version and architecture:
python -m pip download -r requirements-dev.txt -d .\wheels

# On the target:
.\scripts\setup.ps1 -Offline -Wheelhouse .\wheels -Dev
```

A wheelhouse is platform- and Python-version-specific. `pip download` for
Windows/Python 3.14 must be performed with a compatible platform or a compatible
interpreter.

### `Tkinter` is missing

The text console still works:

```powershell
.\scripts\launch.ps1 --cli
```

Install a CPython build that includes Tkinter (the official Windows installer
does by default). This project does not download a GUI toolkit.

### OpenBLAS allocation errors

On constrained or heavily parallel Windows sessions, NumPy/OpenBLAS can print
`Memory allocation still failed after 10 retries`. The application sets
`OPENBLAS_NUM_THREADS=1` and `OMP_NUM_THREADS=1` by default at startup for its
tiny model. The tests do the same. To apply the same setting manually:

```powershell
$env:OPENBLAS_NUM_THREADS = "1"
$env:OMP_NUM_THREADS = "1"
```

Close duplicate Python processes and retry. If the error persists, the process
may be out of commit memory; reboot or inspect memory pressure. The model is
small and does not need parallel BLAS.

### `Configuration error: ...`

Check the YAML syntax and the selected file:

```powershell
$env:PA_CONFIG = "C:\Path\To\config.yaml"
.\scripts\launch.ps1 --diagnose --json
```

`PA_HOME` changes the root; `PA_CONFIG` selects a specific YAML file. The app
also runs with built-in defaults if `config/config.yaml` does not exist. Do not
put passwords or API keys in configuration; there are no secret fields.

Common validation failures:

- `app.offline_mode` must be `strict` or `advisory`;
- `app.auto_approve_max_risk` must be 1-4;
- `speech.asr_engine` must be `template`, `vosk`, `whisper`, or `none`;
- `speech.wake_command_engine` must be `whisper` or `vosk`;
- `speech.wake_command_seconds` must be between 1 and 15 seconds;
- `speech.wake_word_enabled: true` needs the Vosk runtime and model for wake
  detection, but may use either engine for the command itself;
- `speech.wake_word_aliases` must be a list of strings;
- `speech.tts_engine` must be `formant`, `sapi`, or `none`;
- `sapi` requires `speech.allow_os_pretrained_tts: true`;
- file size limits must be between 1,000 and 100,000,000 bytes;
- a dangerous root such as `C:\Windows` is refused unless
  `paths.allow_dangerous_roots` is deliberately enabled.

## Permissions and paths

### "Read access is limited to configured folders" / "Write access is limited..."

The target is outside `paths.read_roots` or `paths.write_roots`. Add the
narrowest folder you actually need, restart, and review
[SECURITY.md](SECURITY.md) first. The default write root is only
`<APP_ROOT>\sandbox`.

### "private internal data" or "protected security data"

`data`, `logs`, `config`, `training_data`, and `models` are protected internal
locations. General file tools cannot read or write them. Use the Memory
inspector, export command, audit log, model scripts, or an OS-level tool as
appropriate. `paths.allow_protected_read: true` is a read-only exception and
does not make those paths writable.

`.env`, private keys, browser credential/cookie files, `.ssh`, `.gnupg`,
`.aws`, Windows `System32`/`SysWOW64`, UNC paths, NTFS alternate data streams,
and reserved device names are blocked by policy.

### A tool is not on the allowlist

`permissions.denied_tools` always wins. `permissions.allowed_tools` defaults to
`["*"]`; if you replaced it with a list, add the tool name or use a broader
reviewed policy. Unknown tools cannot run at all. The catalog is available with:

```powershell
.\scripts\launch.ps1 --list-tools
```

### An action needs approval but there is no window

A one-shot command or non-interactive console denies rather than assuming
consent. Run the interactive GUI/console and approve there, or deliberately
lower the automatic approval threshold with the understanding that this reduces
safety. For a plan, approval is requested once at the highest risk; later steps
are still checked against the scoped grant.

### The kill switch is active

Type `resume` (or use the GUI Resume control). The switch is runtime-only and
does not survive a restart. It is intentional that normal commands cannot clear
it.

## Files, archives, and undo

### `fs.write` refuses to overwrite

Overwrite is off by default (`files.allow_overwrite: false`) and requires an
explicit `overwrite` flag/approval. The tool may back up the old file into the
quarantine/backup area. Rename or delete the old file deliberately, or approve
the overwrite.

### A ZIP extraction is rejected

The extractor enforces zip-slip protection, maximum entry count, maximum
uncompressed size, and compression-ratio limits. These are intentional
zip-bomb/escape defenses. If the archive is trusted and within policy, raise the
specific `files.max_zip_*` limit in configuration after reviewing the reason.

### Delete did not permanently remove the file

Correct: delete means quarantine. Look under `data/quarantine`. `undo` can move
the latest supported operation back. Quarantine retention is not automatically
purged in this build; remove old items deliberately. Backups and quarantine are
not encrypted.

### `undo` says the latest operation is not undoable

Undo only reverses the **latest** eligible journal entry. If the newest entry is
an app launch, typing action, browser action, media key, or another
non-filesystem action, undo refuses rather than skipping to an older entry. Run
`undo` before unrelated actions, or restore from your own backup. The journal is
under `data/recovery/journal.json`; it is not a backup and is not tamper-proof.

### A copy/move was interrupted

Inspect the source, destination, quarantine, and
`data/recovery/journal.json`. A task may be marked `recovery_required` on the
next start if it was interrupted; it is not automatically resumed. Use an
independent backup for important data. The assistant does not repair arbitrary
partial state.

## Applications, windows, and desktop

### `apps.launch` says "unverified"

The launch request was made, but no new matching process/window was observed
within 8 seconds. The app may have opened in an existing instance, been blocked,
or taken longer. Check manually. Do not treat this as verified success.

### An application is not found or is ambiguous

```text
list installed applications
```

Use a more specific name. The resolver accepts reviewed known apps and Start
Menu/App Paths entries. Script files are intentionally rejected by `apps.launch`.
See `apps.capabilities` for known app names.

### Typing went to the wrong window or did not appear

`apps.type_text` sends keystrokes to the current foreground window and cannot
verify the content. Focus the intended window first, or provide the supported
`window_title` argument. The risk is higher for unknown foreground processes.
There is no accessibility-tree verification. Do not use it for privileged or
irreversible forms.

### `windows.open_settings` is unverified

A Settings window was already open or no new window was observed. The page URI
was requested but could not be confirmed. No setting was changed. This tool never
changes Windows settings.

### `windows.open_folder` is unverified

Explorer may have reused an existing window or been blocked. The folder path was
validated before launch. Open it manually if needed.

### Clipboard is busy or empty

Another application may hold the clipboard. Retry after closing it. Reading a
non-text clipboard returns "no text". Clipboard content can contain secrets; do
not capture/export it casually. The tool has no history or automatic backup.

### Screenshot fails

- Pillow must be installed (`pip show pillow`);
- the destination must end in `.png`;
- the path must be inside a write root;
- an existing file needs explicit overwrite approval;
- the desktop session must be available; a disconnected/headless session may
  fail or return a blank image;
- `desktop.screenshot` captures all screens and may include sensitive content.

### Media keys report "unverified"

That is expected. The tool sends the key but cannot read back the player state.
The default media app may not be running.

## Voice

### `voice-templates: No user voice templates are installed`

This is an expected warning when `speech.asr_engine: template` and no phrases
have been recorded. Vosk mode does not need templates. To use template mode,
record the phrases you want, explicitly:

```powershell
.\.venv\Scripts\python.exe .\scripts\record_voice_templates.py --devices
.\.venv\Scripts\python.exe .\scripts\record_voice_templates.py
```

The recorder asks for consent, uses the microphone only during each take, and
stores MFCC features plus a manifest. It does not upload audio. Review the
phrase list with `--list`; remove one with `--remove "exact phrase"`.

### `voice-vosk` says the model is missing

Install the optional local model once:

```powershell
.\.venv\Scripts\python.exe .\scripts\install_voice_model.py
.\.venv\Scripts\python.exe .\scripts\install_voice_model.py --check
```

Then choose `speech.asr_engine: vosk` or `whisper` and restart. The Vosk model is
about 40 MB, Apache-2.0, and stored under `models\vosk`; the Whisper `tiny.en`
model is about 75 MB, MIT, and stored under `models\whisper`. The running
assistant never downloads either. If `vosk` or `faster-whisper` itself is
missing, reinstall dependencies with `scripts\setup.ps1`.

### `voice-whisper` says the model is missing

Install the optional local command model once:

```powershell
.\.venv\Scripts\python.exe .\scripts\install_whisper_model.py
.\.venv\Scripts\python.exe .\scripts\install_whisper_model.py --check
```

The default `tiny.en` model is about 75 MB, MIT-licensed, and stored under
`models\whisper`. The running assistant loads it with `local_files_only=True`;
it never downloads weights. If the model is unavailable, wake commands fall back
to Vosk rather than to a cloud recognizer.

### The Whisper worker fails with `mkl_malloc` or OpenBLAS errors

Vosk and CTranslate2 each carry a native OpenMP/MKL runtime. The application
starts Whisper in a separate local process and sets `CT2_USE_MKL=0`,
`OPENBLAS_NUM_THREADS=1`, and `OMP_NUM_THREADS=1` to keep startup reliable on
Windows systems with a small commit limit or no page file. If model startup still
fails, close memory-heavy applications and retry. This is a local allocation
failure, not a network error; there is no fallback that sends audio elsewhere.
The worker is preloaded when wake listening starts, so the first command is not
delayed by model startup. Vosk itself loads one shared model per process for both
wake detection and push-to-talk, so enabling both does not double its memory use.

### The microphone is unavailable

- Confirm Windows privacy settings allow desktop apps to use the microphone.
- Check `.\scripts\record_voice_templates.py --devices` and `voice.status`.
- Try a different input device by setting `speech.input_device` (an index from
  WinMM enumeration) and restarting.
- A remote desktop or headless session may not expose audio devices.

The tests deliberately do not open the microphone.

### General speech is not recognized

Vosk and Whisper recognize general English locally, but accuracy depends on the
model, microphone quality, distance, and background noise. The installed
configuration uses Vosk's constrained grammar for wake detection and the local
Whisper `tiny.en` model for the command. Speak a complete command such as “open
notepad” or “take a screenshot”. The assistant does not guess when no text is
returned, and it never falls back to a cloud recognizer. If the model returns
unrelated text, the microphone is probably picking up loud ambient audio rather
than your voice; try a headset or quieter room.

Template ASR is **closed vocabulary**, not dictation. It can only match phrases
you recorded. Improve results by recording 2-4 clean takes per phrase, speaking
at a similar distance and pace, reducing background noise, and using the exact
phrase. The recognizer rejects low-confidence or ambiguous matches instead of
guessing. Adjust `speech.template_threshold` / `template_margin` only after
recording more data; loosening them increases false matches.

### The wake word does not trigger

- Wake detection needs the installed Vosk model even when commands use Whisper.
  Enable listening with the `Wake` button, `--wake`, or
  `speech.wake_word_enabled: true`.
- Say “hearty” followed by a short pause, then the command. The small model often
  transcribes it as “hardy”; both forms and several aliases are accepted.
- Wake detection uses a constrained grammar rather than open-vocabulary Vosk,
  because open decoding cannot keep up with live audio on slower CPUs.
- Watch the badge: `wake: loading` means the local command model is starting;
  `wake: listening` means the microphone is open; `wake: busy` means a command or
  speech output is running, and listening resumes afterwards.
- The listener pauses during push-to-talk and typed commands by design to avoid
  recording its own output.
- Check the transcript for a wake-word error, especially a missing model or
  microphone privacy block.
- Stop the listener with `Wake: off` or the kill switch. Only one application
  should hold the microphone in shared mode at a time.

### Speech output is unavailable or sounds robotic

The default formant synthesizer is deterministic and intentionally
from-scratch; it is robotic and has not been perceptually validated. No audio
read-back verifies audibility. Check `voice.status` and the selected output
device. To use an installed Microsoft voice, explicitly opt in:

```yaml
speech:
  tts_engine: sapi
  allow_os_pretrained_tts: true
```

Read [MODEL_POLICY.md](MODEL_POLICY.md) before enabling a pretrained OS voice.
There is no automatic fallback.

## Memory and privacy

### Memory is disabled

Use `enable memory`, the Memory inspector, or set `memory.enabled: true`; restart
if you edited YAML. Disabling memory does not delete existing rows. In a frozen
build, the database is under `%LOCALAPPDATA%\PersonalAssistant\data`, not next to
the executable.

### Memory database is locked

Close other assistant windows/processes and retry. The store uses SQLite WAL and
a busy timeout. A backup or antivirus scanner can also hold the file. Do not edit
the database while the app is running.

### I deleted memory but the data may still exist

Deletion is logical SQLite deletion, not secure erase. WAL files, free pages,
backups, exports, volume snapshots, and disk forensics may retain content.
`forget all=true` runs `VACUUM`, but that is not a guarantee. Use device
encryption and a retention policy. Export files are ordinary JSON and contain
personal data.

### I want to inspect or export memory

Use the GUI Memory inspector or:

```text
what do you remember
export memory to memory.json
```

The export destination must be inside a write root. Treat the file as private.
The audit log is separate and records structured events, not full command text.

### Logs or audit records contain sensitive data

Audit redaction is best-effort and pattern-based. A path, filename, free-form
message, or unusual secret format may remain. Do not paste credentials. You can
delete or rotate the local audit log yourself while the application is stopped;
there is no remote log service. Review [SECURITY.md](SECURITY.md) before sharing
logs with anyone.

## Intent model and parser

### The parser asks for clarification

That is the intended safe behavior. The deterministic grammar requires known
actions and arguments. Rephrase with a supported pattern from `help` or
`README.md`. The model only suggests a label after rule failure and only when
the confidence/margin gate passes; it never guesses arguments.

### I want to retrain the model

```powershell
.\.venv\Scripts\python.exe .\scripts\generate_training_data.py
.\.venv\Scripts\python.exe .\scripts\train_intent_model.py
.\.venv\Scripts\python.exe .\scripts\evaluate_intent_model.py
```

To include only approved local examples, add
`--include-user-examples` to the training command. The model card and metrics
will be rewritten. The test split is not read during training. Do not expect
general-language accuracy from a template-trained model; see
[MODEL_POLICY.md](MODEL_POLICY.md).

### The model is missing or unreadable

The parser falls back to deterministic rules and reports the model status in
diagnostics. It does not download a model. Restore a reviewed local model or
retrain. In a frozen build, the bundled model is copied once into the per-user
model folder if no user model exists; a user-trained file is never overwritten.

## UI, diagnostics, and tests

### The GUI will not start

Run:

```powershell
.\scripts\launch.ps1 --diagnose
.\scripts\launch.ps1 --cli
```

If Tkinter or the desktop session is unavailable, use the console. A remote or
service session may not have an interactive desktop. Check `logs/` for the
traceback, but do not paste secrets into a public report.

### `--diagnose` says `voice-templates`, `voice-vosk`, or `voice-whisper` warning

In template mode, `voice-templates` is expected until you record templates. In
Vosk mode, `voice-vosk` is a warning if the optional model is not installed; it
does not fail setup. In Whisper mode, `voice-whisper` is a warning until the
optional command model is installed. Install either with
`scripts\install_voice_model.py` or `scripts\install_whisper_model.py`, or switch
to template mode. Wake detection always needs the Vosk model. The `wake-word`
diagnostic reports whether listening is enabled and whether the Vosk model needed
for detection is present; it is informational when disabled and a warning when
enabled but unusable. The diagnostic is considered healthy when there are zero
errors; warnings still mean a feature is unavailable.

### `--diagnose` says the offline guard is not installed

In strict mode the diagnostic installs and reports the guard. If the check
fails, inspect the traceback and Python environment. `--offline-check` performs
the active external-connection test. A passing Python guard does **not** cover
child processes or browsers.

### The test suite fails on symlink creation

`test_path_policy_resolves_symlink_escape` is skipped when Windows refuses
symlink creation for the current session (for example, Developer Mode off and no
elevation). The policy code still resolves links defensively. Do not treat the
skip as a pass of symlink behavior; use an environment that permits links if
that specific control matters to you.

### Tests fail with OpenBLAS or memory errors

Set the thread limits shown above and close duplicate Python processes. The
shared test fixture also sets `OPENBLAS_NUM_THREADS=1` and `OMP_NUM_THREADS=1`.
Run the suite from the project root with the project interpreter:

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

### `scripts\test.ps1` cannot find `.venv`

Run `.\scripts\setup.ps1 -Dev` first. The script does not install dependencies
implicitly.

## Packaging

### `package.ps1` exits with code 2

PyInstaller is not installed in the recorded environment and was not assumed to
support CPython 3.14. The script reports that instead of fabricating a build.
Install a compatible PyInstaller explicitly, then retry:

```powershell
.\.venv\Scripts\python.exe -m pip install "pyinstaller>=6.15,<7"
.\scripts\package.ps1 -SkipTests
```

Check PyInstaller's current Python support rather than assuming 3.14 works. The
spec builds a one-folder GUI executable and a console executable. A source
bundle is available without PyInstaller:

```powershell
.\scripts\bundle.ps1
```

The bundle excludes `.venv`, runtime data, logs, sandbox files, personal config,
user voice recordings, and the optional Vosk/Whisper models. Both model
installers are included; run them after setup if you want local general speech.
It includes the from-scratch model and synthetic training
data. Verify `checksums.sha256` before using a bundle from another machine.

### The packaged app cannot find the model

The frozen app uses `%LOCALAPPDATA%\PersonalAssistant` by default. A bundled
model is copied from next to the executable (or `_internal\models`) only when the
target model is absent. A user-trained model is never overwritten. Set
`PA_HOME`/`PA_CONFIG` for a different layout, or copy a reviewed model into the
per-user `models` folder. `package.ps1` also copies `models/intent_*` next to the
executable.

## Still blocked

If none of the above resolves the issue, collect:

- the exact command and whether it was typed or spoken;
- the task state and step messages from the UI or `--command`;
- `--diagnose --json` output with personal paths redacted as needed;
- relevant `logs/` lines and the audit event names (not secrets);
- Windows version, Python version, and whether this is a source run or a frozen
  build.

Record the limitation honestly. Do not claim a microphone, TTS, app launch,
screenshot, or packaged-build acceptance that was not actually performed.
