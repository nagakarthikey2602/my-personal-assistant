# Roadmap

This is a deliberately conservative roadmap. Items are not commitments or
dates; they are the work required to turn the current source build into a more
complete, more defensible local assistant. The current build is usable for the
verified workflows in [CAPABILITIES.md](CAPABILITIES.md) and honest about the
rest.

## Current milestone: complete source build

Delivered:

- local typed console and Tkinter desktop UI;
- 53 registered tools across files/documents, applications, Windows status,
  system/desktop, developer tasks, browser opening, memory, and voice;
- deterministic parser/planner, local intent fallback, task state machine,
  verification, approvals, path policy, offline guard, audit log, recovery
  journal, quarantine, undo, memory, CLI/diagnostics/self-test, tests, docs, and
  setup/launch/bundle/package scripts;
- a from-scratch intent model with a recorded model card and honest synthetic
  evaluation;
- a closed-vocabulary, user-recorded speech recognizer and a deterministic
  formant synthesizer.

Known gaps are listed in [CAPABILITIES.md](CAPABILITIES.md), not hidden as
future promises. The most important are packaging verification, real microphone
and perceptual TTS validation, full UI Automation, general mouse control, OS
setting changes, and the Android bridge (not started).

## Phase 1: hardening and reproducible delivery

Priorities:

1. **Packaging verification.** Build and test the PyInstaller one-folder app on
   a Python version PyInstaller actually supports; verify model seeding, Tk,
   audio, paths, and console flags from the packaged layout. Do not mark the
   package supported until this is run and recorded.
2. **Signed/documented artifacts.** Add SHA-256 manifests for release builds,
   reproducible build instructions, and a short provenance record. Code signing
   is optional but recommended for distribution.
3. **Firewall guidance or helper.** Decide whether to ship an optional,
   administrator-only OS firewall helper. Any helper must be explicit, reversible,
   tested, and never silently modify Windows Firewall.
4. **Symlink/junction acceptance.** Run the skip test in a session that permits
   links, add junction coverage, and document the TOCTOU limit. Do not claim
   complete link safety without this.
5. **Quarantine retention.** Either implement deliberate, logged cleanup for
   `files.quarantine_keep_days` or remove the setting. Any purge must not make
   "quarantine, never permanent delete" misleading.
6. **Dependency and vulnerability review.** Pin, audit, and document licenses and
   advisories for NumPy, Pillow, pypdf, psutil, PyYAML, and the dev toolchain.
   Add a local offline dependency scan if practical.
7. **Test coverage and fault injection.** Add coverage for malformed audit/logs,
   disk-full behavior, read-only files, interrupted writes, database corruption,
   and Windows API failures. Keep the side-effect-limited test policy.

Exit criteria: a packaged build and source build both pass the documented
acceptance run; no unsupported claim in README; security boundaries reviewed.

## Phase 2: voice quality and evaluation

1. Record a real microphone acceptance set on the target machine (explicit
   owner consent), including quiet/noisy conditions and several speakers only if
   the owner intends multi-speaker use.
2. Measure template ASR false-accept/false-reject rates per phrase, not just a
   single accuracy number. Report the closed vocabulary and the threshold.
3. Perceptually evaluate the formant TTS for intelligibility and add a documented
   listening protocol. Improve the synthesizer only if it remains deterministic
   and local.
4. Improve the recording workflow: device selection, take management, phrase
   metadata, and a clear delete/export path for voice features.
5. Keep SAPI as an explicit opt-in exception, or remove it if the project decides
   the pretrained-model exception is not acceptable.

Do **not** silently add Whisper, a cloud STT service, or a downloaded TTS model.
A future local neural model would require a model card, a size/latency budget, a
provenance review, and an offline installation path.

## Phase 3: verified desktop automation

1. Add read-only UI Automation (UIA) tree inspection for supported apps: control
   names, types, values, enabled state, and window identity.
2. Use UIA to verify typed text, button states, and application content where
   the app exposes it. Keep a fallback that reports `unverified` rather than
   claiming success.
3. Add a narrowly scoped mouse/click capability with explicit approval, target
   verification, and a kill switch. Do not add a general computer-use agent.
4. Add window resize/move and keyboard shortcut support only with a reviewed
   allowlist and clear risk levels.
5. Improve app resolution and ambiguity handling; never launch a script or an
   unverified executable through `apps.launch`.
6. Add robust tests that mock the desktop; keep real launch/type/close actions
   behind explicit consent.

Exit criteria: every new action states whether it was verified by UIA, observed
by window/process evidence, or unverified. No implicit click/type actions.

## Phase 4: browser and document work

1. Decide whether a local-only browser integration is worthwhile. Any browser
   automation must not download a browser, bypass the offline guard, or treat
   page text as instructions.
2. If implemented, use a local WebView2/host with an explicit origin allowlist,
   no credentials, no form filling by default, and per-action approval.
3. Add PDF metadata/text extraction improvements and DOCX formatting only with a
   parser that is local, tested, and does not execute macros.
4. Add OCR only as an explicitly optional local component with a model card; it
   is not in the current build.

Exit criteria: page content remains untrusted; external navigation is opt-in;
there is no hidden network path.

## Phase 5: Windows settings and system changes

Read-only status is already implemented. If setting changes are added:

- one explicit tool per setting family, with a strict schema and dynamic risk;
- show old and new values before approval;
- record a verified restore/undo recipe where Windows exposes one;
- refuse startup/service/security changes by default;
- test with a disposable account or VM;
- never make an unreviewed registry edit.

This phase is lower priority than verification and packaging because it has a
large blast radius and limited undo.

## Phase 6: authenticated Android bridge (future only)

This is **not implemented** and no server is started. A future bridge must meet
the requirements in [ANDROID_INTEGRATION.md](ANDROID_INTEGRATION.md):

- no unauthenticated listener;
- pairing and token/key rotation;
- TLS or USB/ADB loopback transport with mutual authentication;
- the same registry, path policy, risk approval, kill switch, audit, and
  per-task limits as the desktop;
- no arbitrary shell, file traversal, or approval bypass;
- explicit opt-in, bind address limits, rate limits, replay protection, and
  remote revocation.

Until that work is complete, `integration.android_enabled` remains `false` and
there is no remote API.

## Phase 7: local intelligence

1. Expand reviewed, non-synthetic evaluation data and report per-label
   precision/recall, calibration, and abstention separately.
2. Improve confidence calibration and the clarify-versus-act decision; keep the
   deterministic parser as the authority.
3. Support user-approved examples with explicit consent, provenance, and a
   retraining report.
4. Consider a small local language model only if it can run offline on the
   target hardware, has a clear model card, does not execute tools, and does not
   replace the registry/approval path.
5. Keep the ability to disable all model use and keep the assistant functional.

## Non-goals

- cloud AI, telemetry, remote logging, or silent online fallback;
- an unauthenticated remote control server;
- an always-listening microphone or general dictation in the current milestone;
- arbitrary shell access or a general computer-use agent;
- permanent delete, ransomware rollback, or a claim of secure erase;
- OS-level sandboxing or protection from a compromised Windows account;
- pretending an unverified action succeeded.

## Definition of done for a future milestone

A milestone is done only when:

- the capability is implemented, not merely documented;
- policy, approval, verification, timeout/cancel, and audit paths are covered;
- tests are local and side-effect-limited unless explicit consent is recorded;
- failures are reported honestly and do not leave a false success state;
- docs state the limits and the exact measured environment;
- packaging and dependency/provenance records are updated.
