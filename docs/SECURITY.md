# Security and privacy

**Status:** source build `0.1.0`, exercised on Windows 11. This is a defensive
local application, not a sandbox, hardened security product, or audited
security boundary. The controls below are implemented and tested to the extent
described in [ACCEPTANCE.md](ACCEPTANCE.md); they are not a guarantee against a
malicious local user or a compromised Windows account.

## Scope and dangerous assumptions

The assistant runs as the signed-in Windows user, with that user's files,
network identity, registry access, and desktop session. It has **no separate
privilege boundary**. Anyone who can run code as that user can read the same
files, modify the audit log, replace the model, or change the configuration.

The design assumes:

- the Windows account, disk, and operating system are trusted;
- the owner reviews configuration changes;
- commands and file content may be untrusted and must not be executed as code;
- the owner does not deliberately enable a dangerous path root or a
  pretrained-model exception without understanding the result.

The design does **not** protect against:

- a local administrator, malware, or another process running as the same user;
- a stolen unlocked computer;
- a compromised Python installation, dependency, or Windows component;
- physical access, disk theft, or forensic recovery of deleted data;
- a denial-of-service attack against the desktop session;
- vulnerabilities in third-party applications that the assistant launches.

## Threat model and controls

| Threat | Control | Important limit |
|---|---|---|
| A command is misunderstood and does something unexpected | Closed, deterministic parser; explicit clarification; risk approval; tool registry | Natural language is inherently ambiguous. The model never supplies arguments. |
| A model output invents a tool or arguments | Model may only suggest a label; arguments come from deterministic text rules; registry/schema checks | A label can still route to the wrong parser handler. Confirmation is required for high risk. |
| A document or web page contains injection text | Content is treated as data, never as instructions or executable code | The assistant does not read arbitrary page text into the planner; browser automation is absent. |
| Path traversal, a junction/symlink escape, UNC, or NTFS alternate data stream | Canonicalisation, root checks, UNC/ADS/device-name rejection, protected roots, deny lists | This is not a filesystem sandbox; a local attacker can race a check or modify a link. |
| Sensitive files are read or overwritten | Read/write allowlists; denied names/fragments; protected internal roots; quarantine delete; overwrite approval | Allowlists are only as narrow as the configured roots. Name matching is not content classification. |
| Network egress | Strict in-process socket guard; browser gates; no runtime downloader | Child processes, browsers, native libraries, and other processes are outside the guard. |
| A high-impact action runs unattended | Risk levels, allow/deny lists, preflight approval, confirmation flags, CLI denial when non-interactive | A user can deliberately loosen policy. Auto-approved risk 2 actions still have side effects. |
| Runaway or unwanted actions | Kill switch, cancellation tokens, task timeout, step limit, process-tree termination for `dev.run` | Cancellation is cooperative; a blocking tool may finish before it notices. |
| Evidence is lost or a change cannot be reversed | Quarantine, backup-before-overwrite, recovery journal, verification, undo | Undo covers selected filesystem actions only. It is not a backup system. |
| Personal data is exposed in logs | Structured audit fields, best-effort secret redaction, default `store_outputs: false` | Redaction is pattern-based. Free-form text can still contain personal or secret data. |
| Memory data is read by another local account | Windows account ACLs and standard file permissions | The SQLite database and journal are **not encrypted**. Disk encryption is the owner's responsibility. |

## Tool execution gate

Every action that can affect the PC must resolve through the registered tool
catalog:

1. the kill switch and cancellation token are checked;
2. the tool must exist in the immutable-in-process registry;
3. arguments are validated against a strict JSON-Schema subset
   (`additionalProperties: false`, types, bounds, enums, patterns);
4. `permissions.allowed_tools` and `permissions.denied_tools` are applied;
5. network-requiring tools are rejected in strict offline mode;
6. dynamic risk is calculated and approval is requested if required;
7. the handler runs with the local context;
8. known failures become structured results, not fabricated success;
9. request, denial, finish, and cancellation events are written to the audit
   log.

There is no arbitrary shell tool. `dev.run` is limited to the exact commands in
`tools.allowed_commands`, with no shell, no extra arguments, and no command
chaining.

## Risk levels and approval defaults

| Level | Label | Examples | Default |
|---|---|---|---|
| 1 | read-only | list files, status, inspect memory | automatic |
| 2 | reversible | write a new file, copy, move, launch a known app, clipboard write, screenshot | automatic |
| 3 | sensitive | quarantine delete, external browser navigation, app close, ZIP extraction, memory deletion | explicit approval |
| 4 | privileged/high-impact | force-kill an application | explicit approval |

`app.auto_approve_max_risk` defaults to `2`. `app.confirm_level_3` and
`app.confirm_level_4` default to `true`. Raising the auto-approval level does
not override either confirmation flag; all three settings must be considered.
The UI and CLI show a tool name, title, risk label, argument preview, reason,
and whether the filesystem undo journal covers the action. Approval requests
time out to denial (120 seconds by default), and a non-interactive CLI denies
rather than assuming consent.

**Risk 2 is not harmless.** Launching an installed application runs that
application with the user's privileges. A screenshot can capture secrets.
Writing a file can overwrite data if explicitly approved. Reduce the automatic
level to `1`, and use explicit allow/deny lists, if the machine is shared or the
workload is sensitive.

A task preflights the highest-risk step and asks once for the plan. Each step is
still checked against the task-scoped grant; a step whose effective risk exceeds
the grant triggers authorization again.

## Filesystem policy

The default write root is only:

```text
<APP_ROOT>\sandbox
```

The default read roots are the application root plus the user's `Downloads`,
`Documents`, and `Desktop` folders. Writes are never implied by a read root.
Internal runtime locations are protected:

```text
<APP_ROOT>\data
<APP_ROOT>\logs
<APP_ROOT>\config
<APP_ROOT>\training_data
<APP_ROOT>\models
```

These paths are blocked from general file tools even if they are inside a read
root. `paths.allow_protected_read: true` is a read-only exception for inspection;
it never makes a protected path writable. The model and speech-template files
are internal even when they are not personal documents.

The path policy also:

- resolves existing symlinks and junctions before checking the allowlist;
- rejects UNC paths, NTFS alternate data streams, reserved device names, NUL
  bytes, and overlong paths;
- blocks sensitive names such as `.env`, private keys, `.pfx`, `.p12`, `.kdbx`,
  browser login/cookie stores, and the credential/SSH/GnuPG/AWS fragments in the
  default deny list;
- blocks the Windows `System32` and `SysWOW64` fragments;
- refuses dangerous root entries such as `C:\Windows`, `C:\Program Files`, and
  `C:\ProgramData` unless `paths.allow_dangerous_roots` is explicitly enabled.

Limits:

- The deny list is name- and fragment-based, not content classification. It
  cannot identify every secret-bearing file.
- A very broad read root (for example all of `C:\Users`) exposes more than the
  defaults. Add the narrowest folder that meets the need.
- The check and the filesystem operation are separate operations. A local
  attacker with write access to a path could race a link replacement. The
  assistant is not a kernel sandbox.
- `paths.allow_dangerous_roots` bypasses a configuration guard. Do not enable it
  unless you have reviewed the impact.

Delete means **quarantine**, not permanent deletion. The tool moves the item
under `paths.quarantine` and records an undo action. It does not call a
permanent-delete API. Quarantine and backup folders can still grow without
bound: `files.quarantine_keep_days` is a documented retention intention, but
automatic purge is **not implemented** in this build. Remove old quarantine
content deliberately, or use full-disk encryption and a retention policy.

## Offline guarantee and its boundary

With `app.offline_mode: strict` (the default), `Assistant` installs a Python
socket guard. It blocks non-loopback `connect`, `connect_ex`, `sendto`, `bind`,
`getaddrinfo`, and `create_connection` calls. Loopback is allowed for local
tests and IPC. `--offline-check` installs the guard, attempts a non-loopback
connection to the TEST-NET address `192.0.2.1:9`, and verifies loopback. The
diagnostic command also installs the guard in strict mode so its report reflects
an actual installation.

**This is an application-level guard, not an OS firewall.** It does not cover:

- child processes, including `dev.run` commands and their imports;
- `os.startfile`, the default browser, and page navigation outside the Python
  process;
- Windows/native libraries and audio components;
- SAPI speech through a PowerShell child process (if explicitly enabled);
- other applications, services, or traffic generated outside the assistant.

`dev.run` sets `PA_OFFLINE=1` for its child environment, but that is an
environment hint, not an enforcement mechanism. Running a test suite can execute
arbitrary local test code with the user's privileges.

Browser behavior has two independent gates:

1. strict offline mode rejects every external URL, regardless of the browser
   setting;
2. outside strict mode, `tools.browser_network_enabled` must be explicitly
   `true`, and the URL scheme must be in `tools.browser_allowed_schemes`.

Local `file://` pages are allowed only inside the read allowlist. They are
opened in the default browser, so the browser process itself is outside the
Python guard. Page content is untrusted data. There is no form filling,
clicking, download handling, or page-content-to-action path.

Setup (`scripts/setup.ps1`) is the only provided step that may use the network
for pip downloads. The runtime has no downloader, updater, telemetry client, or
cloud fallback. Use `-Offline -Wheelhouse <dir>` for an air-gapped installation.

### Optional OS-level enforcement

If the application-level boundary is insufficient, create an outbound Windows
Firewall rule for the exact interpreter or packaged executable **after**
reviewing it and preparing updates. This is a system change; it is not performed
automatically by the project. An administrator PowerShell example is:

```powershell
# Review the exact path first. This blocks almost all outbound traffic for that
# executable; loopback traffic is normally exempt from Windows Firewall.
New-NetFirewallRule -DisplayName "Local Personal Assistant outbound block" `
  -Direction Outbound -Program "C:\my personal assistant\.venv\Scripts\python.exe" `
  -Action Block -Profile Any

# Remove it for updates or diagnostics that need package installation:
Remove-NetFirewallRule -DisplayName "Local Personal Assistant outbound block"
```

An OS rule for `python.exe` affects every use of that interpreter, including
pip. Apply it after setup, keep a documented removal procedure, and do not
mistake it for a complete machine-wide egress control. A separate firewall,
network segmentation, or offline VM is stronger.

## Audit log

The default audit path is `<APP_ROOT>\logs\audit.jsonl`. Records are append-only
JSON Lines with a UTC timestamp, event name, and structured fields. Events
include command reception (character count, not command text), task start and
finish, tool requested, tool denied, tool finished, tool cancelled, and
assistant start. Tool request arguments are passed through a best-effort
redactor.

The log is **not**:

- encrypted, signed, or tamper-evident;
- protected from the same Windows user;
- a complete record of OS activity or child-process output;
- guaranteed to contain every secret pattern.

Redaction covers keys containing `password`, `secret`, `token`, `api_key`, or
`apikey`, plus common `password=...`, bearer-token, and private-key patterns, and
bounds values to 400 characters by default. A secret in a different format, a
free-form message, a path, or a filename can still be logged. Do not paste
credentials into commands.

The log rotates when it would exceed `permissions.audit_max_megabytes` (10 MB by
default): the current file becomes `audit.jsonl.1`, and an existing `.1` is
deleted. Rotation errors are swallowed; append errors are not. There is no
external log shipper and no central monitoring.

## Kill switch, cancellation, and timeouts

The kill switch is a runtime-only switch. `kill switch`/`stop` halts all new
tool execution, cancels active tokens, and makes normal commands return an error
until `resume` is used. It is not persisted across restart, so restarting the
application clears it. The switch is a safety control, not a forensic lock.

Cancellation is cooperative:

- the task engine checks the token before and after steps;
- `dev.run` checks every 0.2 seconds and terminates its process tree;
- long native calls that do not check the token may complete anyway;
- the task timer sets the cancellation token but cannot forcibly unwind every
  handler;
- shutdown cancels tokens and stops audio where the service is active.

`app.max_task_seconds` defaults to 600 and `app.max_steps` to 40. The step count
is a hard plan limit. The timeout is a cooperative limit, not a guarantee that a
stuck native API returns immediately.

## Memory and personal data

The SQLite database is at `<APP_ROOT>\data\assistant.db` and may have
`-wal`/`-shm` companions. It is local and **unencrypted**. It can contain:

- explicit preferences;
- task records and step records, including tool arguments;
- command text when `memory.store_commands` is true (the default);
- summaries and short step messages; full tool output is not stored when
  `memory.store_outputs` is false (the default), but the summary is still
  stored and truncated to 1,000 characters;
- training examples explicitly added by the user.

The default retention is 90 days and `max_history` 1,000 tasks. Retention is
applied when memory is initialized and history is trimmed after task writes;
there is no background purge while the application is idle. `memory.status`,
`memory.list`, `memory.export`, `memory.forget`, and the Memory inspector expose
or delete records. `memory.set_enabled: false` stops new storage but does not
delete existing records.

`memory.export` writes a JSON file into an allowed write root. That file contains
personal data and is protected only by its location and Windows ACLs. Export does
not encrypt it. Deletion is logical/SQLite deletion, not a secure erase: free
pages, WAL files, backups, volume snapshots, and previous exports may retain
data. `memory.forget all=true` runs `VACUUM`, which reduces but does not
guarantee physical erasure. Use BitLocker/device encryption, a dedicated Windows
account, and a documented retention/export policy if the data is sensitive.

The local intent model is a small from-scratch classifier. User-approved
training examples are not used by the running parser automatically; they are
included only when the owner runs `scripts/train_intent_model.py
--include-user-examples`. The default recorded model included zero user
examples.

## Voice and audio privacy

- The microphone is opened only for explicit push-to-talk in the GUI/console or
  for an explicitly started template-recording script. There is no wake word,
  always-listening mode, or background capture.
- Audio is processed locally. Template recording stores MFCC features and a
  manifest under `<APP_ROOT>\models\speech_templates`, not raw audio, according
  to the current script. Treat the features and phrase text as personal data.
- The default formant TTS is deterministic and local. It sounds robotic; this
  is a quality limitation, not a privacy claim.
- SAPI is a Microsoft-pretrained OS voice. It is disabled by default and
  requires both `speech.tts_engine: sapi` and
  `speech.allow_os_pretrained_tts: true`. When enabled, speech is synthesized by
  a PowerShell child process using installed Windows voices. There is no
  automatic fallback from formant to SAPI. See [MODEL_POLICY.md](MODEL_POLICY.md).

## Desktop and application risk

- `apps.launch` runs an installed/known application as the user. It is risk 2
  and auto-approved by default. The application's own network, file, and
  privilege behavior is outside the assistant's guard.
- `apps.type_text` sends Unicode keystrokes to the foreground window. It is
  unverified; it uses a higher risk for non-editor foreground windows, but no
  accessibility-tree content verification is available.
- `apps.close` is risk 3; `force=true` is risk 4.
- `windows.open_settings` only opens a settings page. It does not change OS
  settings or startup entries. `windows.open_folder` verifies a window appeared.
- `desktop.clipboard_get` can expose whatever another application put on the
  clipboard. It is read-only risk 1 and is not written to the audit log by the
  tool, but the result is displayed and may be summarized in memory if command
  text or arguments contain it. `desktop.clipboard_set` replaces the clipboard.
- `desktop.screenshot` captures **all screens**, not one window. It can capture
  secrets, notifications, and other users' content. It is risk 2 and auto-
  approved by default; disable it with `tools.screenshot_enabled: false` or
  raise the automatic approval threshold.
- Media keys and browser navigation report `unverified` because the backend
  cannot read back the resulting state.

## Recommended hardening

1. Keep `app.offline_mode: strict`, `tools.browser_network_enabled: false`, and
   `permissions.allowed_tools` as an explicit list rather than `"*"`.
2. Set `app.auto_approve_max_risk: 1` and keep `confirm_level_3`/`4` enabled on
   shared or high-value machines.
3. Remove broad read roots; never add system directories or the whole user
   profile. Keep `paths.allow_dangerous_roots: false`.
4. Disable unused tools: `tools.terminal_enabled`, `tools.screenshot_enabled`,
   `tools.clipboard_enabled`, `tools.ui_automation_enabled`, or individual
   entries in `permissions.denied_tools`.
5. Keep `memory.store_commands` and `store_outputs` as narrow as the use case
   allows; lower retention; export only to a protected location.
6. Enable BitLocker/device encryption and use a dedicated Windows account.
7. Review `logs/audit.jsonl` and the recovery journal; keep their permissions
   restricted. Back up important files independently of the assistant.
8. Use an OS firewall or network isolation if the Python-level guard is not the
   boundary you need. Verify with `--offline-check` and, separately, with an
   external network monitor.
9. Keep the interpreter, dependencies, Windows, and launched applications
   patched. The bundled lock file is a reproducibility record, not a
   vulnerability guarantee.
10. Before enabling SAPI or external browser navigation, read
    [MODEL_POLICY.md](MODEL_POLICY.md) and accept the changed boundary.

## Incident response

If the assistant appears compromised or has performed an unexpected action:

1. Activate the kill switch, stop the process/window, and close the session.
2. Disconnect the machine from the network if exfiltration is possible.
3. Preserve `logs/`, `data/recovery/journal.json`, the SQLite database, and
   relevant Windows event logs before cleanup.
4. Review `audit.jsonl`, the recovery journal, memory, recent commands, and
   launched applications. The audit log is not tamper-proof; absence of a
   record is not proof of absence.
5. Rotate any credentials that may have appeared in commands, clipboard content,
   screenshots, exports, or logs.
6. Remove or quarantine suspect files, restore from an independent backup, and
   reinstall dependencies from a reviewed wheelhouse if necessary.
7. Re-run `--diagnose`, `--offline-check`, and the test suite from a clean
   environment; do not trust a modified `.venv` or `PA_HOME`.

There is no hosted security response team or warranty. This is a local
source project; record findings privately and review the code and configuration
before relying on it for high-value data.
