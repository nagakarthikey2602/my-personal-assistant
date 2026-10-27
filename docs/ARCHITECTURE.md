# Architecture

## System shape

The assistant is a single-process, local Windows application. Tkinter and the
console are thin clients over one orchestration object. Every action resolves
through the same tool registry and execution gate.

```text
Tkinter window / console / one-shot command
                    |
              Assistant (assistant.py)
        parse -> plan -> task state machine
                    |
             ToolExecutor gate
   schema -> allowlist -> risk approval -> kill switch
                    |
              registered tool handler
                    |
     verification -> recovery journal -> audit -> memory
```

There is no remote API, message broker, cloud call, or background network
service. Memory, logs, configuration, and the model are local files.

## Startup and configuration

`Config` merges built-in defaults with an optional YAML file. Source runs use the
repository root, or `PA_HOME` when set. A frozen build uses
`%LOCALAPPDATA%\PersonalAssistant` unless `PA_HOME` overrides it.
`PA_CONFIG` can select a different YAML file.

Relative paths and the `<APP_ROOT>` / `<HOME>` markers are resolved before any
tool sees them. `ensure_directories()` creates the data, log, model, quarantine,
backup, temporary, database, recovery, speech-template, and write-root
directories. A frozen build calls `seed_bundled_models()` once to copy a model
shipped next to the executable into the per-user model folder if no model exists.
This is a local file copy only; it never overwrites a user-trained model.

Strict offline mode installs the Python socket guard during `Assistant`
construction. `install_guard=False` is used by tests and by self-test code that
must remain isolated.

## Command path

1. **Parser.** A deterministic grammar handles builtins (`help`, `status`,
   `capabilities`, kill switch, resume), file/app/system/voice/memory actions,
   and composite commands joined by `then`. Missing or ambiguous details produce
   a clarification, not a guess.
2. **Model fallback.** If the rule parser marks an utterance unknown, the local
   softmax classifier may suggest a label. A strict confidence and margin gate
   must pass. The label only selects a parser handler; that handler still derives
   arguments from deterministic text rules. The model cannot invent arguments,
   grant approval, or call a tool directly. Low-confidence model suggestions
   produce a clarification.
3. **Planner.** The selected intent becomes an ordered `ExecutionPlan` of
   `PlanStep`s. Each step names a registered tool, validated arguments, a title,
   and a risk level. The plan step count is limited by `app.max_steps`.
4. **Task engine.** `TaskEngine` performs a preflight approval for the highest-
   risk step, applies timeouts and cancellation, executes steps in order, and
   records a terminal state (`completed`, `failed`, `blocked`, `cancelled`). The
   outcome is persisted to memory if memory is enabled.
5. **Executor gate.** For every step the executor:
   - checks the kill switch and cancellation token;
   - resolves the registered `ToolSpec` (unknown tools cannot run);
   - validates arguments with the strict JSON-Schema subset;
   - applies `permissions.allowed_tools` / `denied_tools`;
   - rejects network-requiring tools in strict mode;
   - computes dynamic risk and requests approval when required;
   - runs the handler with a `ToolContext`;
   - converts known failures into structured results;
   - logs request, denial, finish, and cancellation events.

The default risk policy auto-approves levels 1 and 2 and requires explicit
approval for levels 3 and 4. `preflight_plan` asks once for the highest risk in
a plan and passes a task-scoped `ApprovalGrant`. The CLI denies when stdin is not
interactive. The Tkinter approval bridge schedules a dialog on the main thread
and resolves it with the user's answer or timeout denial.

## Tools and verification

Tools are grouped in `app/personal_assistant/tools/`:

| Module | Responsibility |
|---|---|
| `filesystem.py` | allowlisted file operations, archive safety, verification, undo recipes |
| `documents.py` | text/PDF/DOCX reading, DOCX writing, injection heuristics |
| `applications.py` | Start Menu discovery, launching, window enumeration/focus, Unicode typing, graceful close |
| `windows.py` | read-only Windows status, Settings-page and Explorer opening |
| `system.py` | hardware/process/battery/audio/offline/media status |
| `desktop.py` | clipboard and screenshot operations |
| `dev.py` | exact command allowlist, process-tree timeout/cancel, log analysis |
| `browser.py` | local file URL and explicit external URL opening |
| `memory_tools.py` | memory/preferences/history/training-example tools |
| `voice_tools.py` | speech status, device listing, local speech output |

`ToolResult` has one of `ok`, `error`, `unverified`, `cancelled`, or
`unavailable`. Verification is intentionally conservative:

- `fs.write/read/copy/move/rename/zip/extract` re-check the filesystem and set
  `verified=True` only after the observed state matches.
- `apps.launch` reports success only when a new process/window is observed;
  otherwise `unverified`.
- `apps.type_text`, media keys, browser navigation, and speech output are
  `unverified` because the backend cannot read back the resulting state.
- A task is verified only when its policy requires and the tool results support
  verification; a plan with an unverified step is reported as such.

## Filesystem undo and recovery

`RecoveryJournal` writes a local JSON journal under `paths.recovery_journal`.
File tools record recipes such as restore backup, remove created path, move back,
or quarantine path. `fs.undo_last` applies the latest recipe and verifies the
result. Delete operations move items to the quarantine root; they do not call a
permanent delete API. Screenshots and archives also record undo recipes where
possible. Application, typing, browser, and media operations are outside the
journal.

The journal is not encrypted and is readable to the same Windows account. It is
local recovery state, not a tamper-proof transaction log.

## Memory

`MemoryStore` uses SQLite at `paths.database`, with schema data packaged under
`personal_assistant/memory/schema.sql`. It stores:

- explicit preferences (`remember`/`forget`);
- task records and step summaries, subject to retention and `max_history`;
- user-approved command examples for training.

`store_commands` controls whether command text is stored; `store_outputs` is
false by default. The store can be disabled without deleting existing rows.
Export produces local JSON. Delete operations are explicit and permanent for the
selected records. There is no encryption at rest; see `SECURITY.md`.

## Speech

`SpeechService` is lazy and uses:

- `speech/native_audio.py`: WinMM waveIn/waveOut through `ctypes`, device
  enumeration, recording, and WAV playback. No background capture.
- `speech/vad.py`: energy-based speech detection and trimming.
- `speech/template_asr.py`: MFCC extraction plus dynamic time warping against
  user-recorded word/phrase templates. Closed vocabulary; no pretrained weights.
- `speech/tts.py`: deterministic formant synthesis. SAPI is an opt-in,
  policy-gated backend and is never selected as a fallback.

The GUI and console route all speech through the same service. Recording only
starts after an explicit push-to-talk or voice-loop action.

## Intelligence

`intelligence/features.py` builds bag-of-words features from locally authored
text. `intelligence/intent_model.py` implements multinomial logistic regression
(softmax) with full-batch Adam and L2 regularization using NumPy. Training and
evaluation scripts live in `scripts/`. The model file contains labels,
vocabulary, weights, and metadata; no pretrained language model is used.

## UI

The Tkinter window (`ui/app.py`) contains a transcript, command entry, voice and
safety buttons, status badges, memory inspector, settings dialog, tool catalog,
and approval dialog. It never calls a tool directly: commands go through
`Assistant.handle_command`, and the approval callback is the shared
`ApprovalBridge`. Long actions run in worker threads; UI updates are scheduled
back onto the Tk main thread. The UI is intentionally not a second policy layer.

## Offline guard

`offline_guard.py` monkey-patches Python socket `connect`, `connect_ex`,
`sendto`, `bind`, `getaddrinfo`, and `create_connection` so non-loopback IPv4/IPv6
addresses are rejected in strict mode. Loopback is allowed for tests and local
IPC. The guard does **not** cover child processes, native libraries, browsers,
or other applications. The browser tool therefore has separate configuration
gates: strict mode blocks external navigation regardless of the browser setting,
and `tools.browser_network_enabled` must also be true outside strict mode.

## Build and test layout

`pyproject.toml` declares runtime dependencies and package data. `scripts/`
contains setup/launch/diagnose/offline/test/bundle/package automation.
`packaging/personal_assistant.spec` builds a one-folder PyInstaller application
with a windowed executable and a console executable; PyInstaller support is not
assumed for every Python version. Tests use pytest fixtures that construct
temporary homes and never touch the owner's runtime data by default.

## Non-goals

This milestone does not implement cloud AI, telemetry, arbitrary shell access,
full UI Automation, browser form automation, OS settings changes, permanent
delete, always-listening voice, general dictation, remote control, or an Android
server. See `CAPABILITIES.md` and `ROADMAP.md`.
