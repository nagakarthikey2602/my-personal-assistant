# Capability matrix and limitations

This is the honest capability list for source build `0.1.0`. "Verified" means
the tool itself performs a post-condition check where the backend permits it.
"Unverified" means the action is requested or submitted but the tool cannot read
back the resulting state. The task result distinguishes these cases; an
`unverified` step is not reported as a fully verified success.

## Status legend

| Status | Meaning |
|---|---|
| Verified | The tool checks the local post-condition (file content/shape, process/window evidence, PNG validity, database result, exit code). |
| Observed | The tool observes indirect evidence, such as a new process or window; it is not a full semantic proof. |
| Unverified | The tool requested the action but cannot verify the final state (typing, browser, media keys, speech output). |
| Read-only | The tool reports a snapshot; it does not change the subject. |
| Not implemented | No code path exists; the assistant must say so rather than pretend. |

## Tool catalog (53 registered tools)

### Files and documents (14)

| Tool | Risk | What it does | Evidence / limit |
|---|---:|---|---|
| `fs.write` | 2 | Write text or a `.docx`; create parents; overwrite only with explicit flag/approval | Re-reads the file; overwrite is a higher-risk action and is backed up where possible |
| `fs.read` | 1 | Read text, PDF, or DOCX within read roots | Content is returned as data; PDF/DOCX extraction is not a rendering engine |
| `fs.list` | 1 | List an allowed folder | Names and metadata only |
| `fs.search` | 1 | Search by name, extension, or text content | Bounded by results/time/file count; no indexing |
| `fs.copy` | 2 | Copy a file/folder and verify contents | Directory fingerprints and hashes where applicable |
| `fs.move` | 2 | Move/rename and verify the destination | Same-volume and cross-volume behavior follows Windows APIs |
| `fs.rename` | 2 | Rename in place and verify | Refuses overwrite by default |
| `fs.quarantine_delete` | 3 | Move up to 100 paths into quarantine | Never calls permanent delete; undo can move back; quarantine cleanup is manual |
| `fs.create_folder` | 2 | Create a folder and missing parents | Verifies the directory exists |
| `fs.zip` | 2 | Create a ZIP of a file/folder | Verifies the archive; records undo |
| `fs.extract` | 3 | Extract a ZIP into a new folder | Blocks zip-slip, excessive entry counts, excessive expansion, and suspicious ratios |
| `fs.duplicates` | 1 | Find duplicate files by size then SHA-256 | Read-only scan; bounded by file count |
| `fs.inventory` | 1 | Produce a file inventory, optionally saved locally | Local scan only; no content classification |
| `fs.undo_last` | 2 | Reverse the latest supported filesystem journal entry | Refuses if the latest operation is not undoable or a file changed |

Document formats: readable text extensions include `.txt`, `.md`, `.json`,
`.jsonl`, `.csv`, `.log`, `.py`, `.ps1`, `.yaml`, `.yml`, `.toml`, `.ini`,
`.cfg`, `.html`, `.htm`, `.js`, `.ts`, `.css`, `.java`, `.c`, `.h`, `.cpp`,
`.cs`, `.sql`, and `.xml`; `.pdf` and `.docx` are read through dedicated
extractors. Writing is text or `.docx`. There is no PDF writer, spreadsheet
engine, OCR, or general binary editor.

### Applications and windows (7)

| Tool | Risk | What it does | Evidence / limit |
|---|---:|---|---|
| `apps.list_installed` | 1 | List Start Menu shortcuts and registered App Paths | Discovery only; entries can be stale or ambiguous |
| `apps.capabilities` | 1 | Describe known app behavior and limits | Static local registry, not a guarantee for every app |
| `apps.launch` | 2 | Launch a known/installed app | Observes a new process/window for up to 8 seconds; otherwise `unverified` |
| `apps.list_windows` | 1 | List visible top-level windows | Visibility only; no accessibility tree |
| `apps.focus_window` | 2 | Focus an exact or partial title | Checks the foreground window; Windows may refuse focus |
| `apps.type_text` | 2-3 | Send Unicode keystrokes to the foreground window | **Unverified**; dynamic risk is 2 for recognized editors, 3 otherwise |
| `apps.close` | 3-4 | Graceful close; force-kill with `force=true` | Observed process exit; force-kill is risk 4 and can lose unsaved work |

There is no general mouse control, click/scroll tool, drag-and-drop, keyboard
shortcut editor, window resize, or accessibility-tree reading. Typing does not
verify the text that arrived. Applications can ignore `WM_CLOSE`, run elevated,
or open in an existing process. Only the exact reviewed command allowlist in
`dev.run` is available for developer work; arbitrary programs are not launched
through it.

### Windows status, settings pages, and Explorer (7)

| Tool | Risk | What it does | Evidence / limit |
|---|---:|---|---|
| `windows.display_info` | 1 | Resolution, monitor count, DPI/scale | Read-only |
| `windows.network_status` | 1 | Interfaces, addresses, Wi-Fi details | Read-only; uses local `netsh`/psutil |
| `windows.bluetooth_status` | 1 | Bluetooth support service state | Read-only; no adapter/pairing management |
| `windows.power_status` | 1 | Active power plan and battery | Read-only; `changing_power_settings` is explicitly not implemented |
| `windows.startup_list` | 1 | HKCU/HKLM run/run-once entries | Read-only; does not change startup entries |
| `windows.open_settings` | 2 | Open one of 13 supported Settings pages | New window = verified; existing window or no window = `unverified`; no setting is changed |
| `windows.open_folder` | 2 | Open a folder in File Explorer | New matching window = verified; otherwise `unverified`; path must be readable |

Supported Settings pages: `home`, `display`, `sound`, `bluetooth`, `network`,
`wifi`, `power`, `notifications`, `apps`, `startup`, `accessibility`, `about`,
and `privacy`. There is no setting mutation, registry editing, service control,
shutdown/restart, Bluetooth pairing, or Wi-Fi credential management.

### System, desktop, and audio (9)

| Tool | Risk | What it does | Evidence / limit |
|---|---:|---|---|
| `system.info` | 1 | Hardware/OS/Python/runtime information | Read-only |
| `system.top_processes` | 1 | Processes by memory or CPU | Snapshot; CPU sampling is short |
| `system.battery` | 1 | Battery/power state | Read-only |
| `system.audio_devices` | 1 | WinMM microphone/speaker devices | Read-only enumeration |
| `system.offline_status` | 1 | Python socket guard status/counts | Reports the application guard, not OS-wide egress |
| `system.media_control` | 2 | Volume/media transport keys | **Unverified**; no read-back of player state |
| `desktop.clipboard_get` | 1 | Read clipboard text | Can expose whatever another app copied; not written to audit by the tool; up to 100,000 characters |
| `desktop.clipboard_set` | 2 | Replace clipboard text | No read-back; the previous clipboard content is not journaled |
| `desktop.screenshot` | 2-3 | Capture all screens to a PNG in a write root | Verifies the PNG; `overwrite` raises risk to 3; screenshots are local and can contain secrets |

### Developer tools (3)

| Tool | Risk | What it does | Evidence / limit |
|---|---:|---|---|
| `dev.run` | 3 | Run one of six exact commands in a writable project folder | Exit code and reviewed output; process-tree timeout/cancel; **child processes are outside the network guard** |
| `dev.analyze_log` | 1 | Extract deterministic failure/context lines | Pattern matching, not causal diagnosis; content is untrusted data |
| `dev.status` | 1 | Show configured command allowlist/availability | Read-only |

Allowed command keys are `pytest`, `unittest`, `compileall`, `git_status`,
`git_log`, and `git_diff`. They map to exact argument vectors; extra arguments,
shell operators, and arbitrary executables are rejected. The configured
canonical list can be narrowed, but the tool cannot invent a command outside the
registry. Running a project's tests still executes that project's code.

### Browser (2)

| Tool | Risk | What it does | Evidence / limit |
|---|---:|---|---|
| `browser.open` | 2 for `file:`, 3 otherwise | Open a local page, or an explicitly enabled external URL | `unverified`; browser window/state is not verified; page content is untrusted |
| `browser.capabilities` | 1 | Describe supported/unsupported browser behavior | Read-only |

Local pages must resolve inside read roots. External navigation is rejected in
strict offline mode regardless of `tools.browser_network_enabled`. Outside strict
mode, the scheme must be allowed and the network flag must be explicitly true.
There is no form filling, page clicking, download handling, cookie/session
management, or page-content-to-tool path.

### Memory (7)

| Tool | Risk | What it does | Evidence / limit |
|---|---:|---|---|
| `memory.remember` | 2 | Store an explicit preference | Local SQLite write; value preview is bounded |
| `memory.list` | 1 | List preferences and recent tasks | Read-only; history limit is bounded |
| `memory.forget` | 2-3 | Delete one preference or all memory records | Permanent logical delete; `all=true` is risk 3; no secure erase |
| `memory.status` | 1 | Enabled state, paths, retention, row counts | Read-only |
| `memory.export` | 2 | Export to a local JSON file | Contains personal data; destination must be a write root; verified by parsing; undo record |
| `memory.set_enabled` | 2 | Stop/start new storage | Does not delete existing rows |
| `memory.add_example` | 2 | Add a training example, optionally approved | Not a preference; not used automatically until a training run |

### Voice (4)

| Tool | Risk | What it does | Evidence / limit |
|---|---:|---|---|
| `voice.status` | 1 | ASR/template availability, TTS backend, devices | Read-only |
| `voice.capabilities` | 1 | Describe the closed-vocabulary limits | Read-only |
| `voice.list_devices` | 1 | Microphone/speaker list | Read-only |
| `voice.speak` | 2 | Speak through the configured local backend | **Unverified**; no audio read-back; formant voice is robotic |

Voice capture is not a standalone tool; it is an explicit push-to-talk action in
the GUI/console. There is no always-listening wake word, speaker diarization,
emotion detection, or general dictation. The template recognizer matches only
phrases the owner recorded. See [MODEL_POLICY.md](MODEL_POLICY.md).

## Example task coverage

These are the main verified local workflows:

- create, read, search, list, copy, move, rename, archive, extract, inventory,
  duplicate-scan, quarantine-delete, and undo supported filesystem actions;
- write and read text/DOCX; read PDF;
- remember/forget/list/export/disable/delete memory;
- inspect display/network/Bluetooth/power/startup/battery/audio/process/runtime
  status;
- open a Settings page or File Explorer folder;
- list, launch, focus, close, and type into applications on a best-effort basis;
- read/write clipboard and capture a screenshot;
- run the six reviewed developer commands and scan a local failure log;
- open local browser pages; external URLs only with explicit opt-in outside
  strict mode;
- speak locally and use the closed-vocabulary voice loop.

## Explicit non-capabilities

The following are **not implemented** and must not be claimed as working:

- general mouse movement, clicking, scrolling, dragging, or computer-use agents;
- UI Automation tree inspection, reading control values, or verified typing;
- browser automation, forms, authentication, downloads, or page text as
  instructions;
- changing Windows settings, registry startup entries, services, network
  configuration, Bluetooth pairing, or power plans;
- permanent deletion, secure erase, forensic cleanup, or ransomware rollback;
- email, calendar, messaging, office-suite automation, or media-library
  management beyond transport keys;
- OCR, image understanding, PDF writing, spreadsheet writing, or arbitrary
  binary editing;
- cloud AI, telemetry, remote logging, remote control, or an Android server;
- general dictation, always-listening wake words, or pretrained speech models
  (SAPI is an explicit, disabled-by-default exception);
- multi-user accounts, encrypted memory, signed/tamper-proof audit logs, or an
  OS-level sandbox;
- a verified PyInstaller executable on every Python version. The packaging path
  is provided, but PyInstaller was absent in the recorded environment.

## Interaction and recovery behavior

- Commands are deterministic and closed-grammar; missing arguments ask for
  clarification. Composite commands can be joined with `then` where the parser
  supports them.
- The highest-risk step in a plan is preflighted once. Approval is scoped to the
  task and cannot be reused by another task.
- Tasks report `completed`, `completed_unverified`, `failed`, `blocked`, or
  `cancelled`, with per-step states. A verified task requires successful,
  verified steps; unverified actions are surfaced as such.
- `undo` applies only the latest supported filesystem journal entry. It refuses
  to overwrite and checks hashes where recorded. It does not undo app launches,
  typing, browser navigation, media keys, or speech.
- `kill switch` and cancellation are cooperative. `dev.run` can terminate its
  process tree; a blocking native call may finish before it observes the token.
- Memory retention is applied at startup and history is trimmed on writes.
  Quarantine retention is not automatically purged in this build.

For installation, setup, diagnostics, and failure recovery, see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md). For the security boundary, see
[SECURITY.md](SECURITY.md).
