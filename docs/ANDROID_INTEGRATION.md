# Android integration architecture (future only)

**Status: not implemented.** The current build starts **no** server, opens no
remote port, and has no Android client. `integration.android_enabled` is
`false`; the settings under `integration` are reserved configuration and are not
consumed by a bridge. This document defines the security requirements for a
future implementation so that no unauthenticated or accidental remote server is
added.

## Non-negotiable requirements

1. **Opt-in only.** A bridge starts only when the owner explicitly enables it in
   configuration and starts a visible session. There is no hidden startup task.
2. **No unauthenticated listener.** Binding a socket without pairing and
   authentication is a defect, not a feature. A default `require_pairing_token:
   true` is mandatory; disabling it must not be a supported mode.
3. **Same authority path.** Remote requests resolve through the existing parser,
   planner, tool registry, path policy, risk approval, offline guard, audit log,
   memory, and task limits. The bridge must not add a direct file API or a second
   policy implementation.
4. **No arbitrary shell.** Remote callers cannot run commands outside the
   reviewed `dev.run` allowlist, and should not receive `dev.run` by default.
5. **Explicit approval.** Sensitive and privileged actions require approval.
   By default, approval must happen on the Windows desktop, not silently on the
   phone. Remote approval, if ever added, requires a second explicit opt-in and
   a local confirmation code.
6. **Local-first transport.** Prefer USB/ADB loopback or a private, authenticated
   tunnel over exposing a LAN port. No cloud relay, public internet exposure, or
   UPnP port mapping.
7. **Fail closed.** Missing keys, expired certificates, malformed messages,
   unknown devices, replay, or policy errors deny the request and are audited.
8. **Kill switch.** The Windows kill switch and task cancellation remain
   authoritative; the bridge must expose a cancel operation and must stop
   accepting work while halted.

## Recommended topology

### Option A: USB with ADB reverse (preferred for a single phone)

```text
Android app --USB/ADB reverse tcp:8765--> 127.0.0.1:8765 on Windows
                                              |
                                       authenticated bridge
                                              |
                                  existing Assistant/tool gate
```

The Windows bridge binds **loopback only**. `adb reverse tcp:8765 tcp:8765`
makes the phone's local port reach the PC loopback service over the trusted USB
debugging channel. The owner enables USB debugging deliberately and can revoke
it. This avoids a LAN listener and is the lowest-exposure starting point.

Limitations: ADB itself is a powerful development interface; do not leave
unauthorized devices authorized, and do not treat USB debugging as a security
boundary against a malicious PC. The Android app still authenticates to the
bridge.

### Option B: private LAN with mutual TLS

Only if USB is impractical:

- bind to a specific private interface, never `0.0.0.0` and never a public
  interface;
- require TLS 1.2+ with a project-generated local CA or pinned self-signed
  certificate;
- require a client certificate or proof of possession of a paired key;
- add a Windows Firewall rule limited to the private profile and the phone's
  address where practical;
- do not use mDNS discovery by default; pairing is explicit (QR/code);
- do not expose the port through a router, tunnel, reverse proxy, or UPnP.

Plain HTTP, a static shared secret in a URL, or a "trusted LAN" assumption is not
acceptable.

## Pairing and authentication design

A future implementation should use:

1. **One-time pairing window.** The owner starts "Pair a phone" on the Windows
   UI. The window is short-lived (for example 2 minutes), visibly indicated, and
   closes after success or timeout.
2. **Out-of-band code.** The PC displays a random one-time code and a
   certificate/public-key fingerprint. The Android app scans a QR code or
   compares the code. The code is never sent over the unauthenticated channel.
3. **Device key pair.** Android generates a key pair in the Android Keystore.
   The public key is sent during pairing. The private key never leaves the
   phone.
4. **Mutual authentication.** The bridge stores the paired public key and device
   ID. The phone pins the PC certificate/public key. Subsequent sessions require
   proof of possession (mTLS or a signed challenge), not just a bearer token.
5. **Token storage.** If a short-lived session token is used in addition to
   mTLS, store only a hash on Windows and protect the phone token with the
   Keystore. Tokens are rotated and revocable.
6. **Revocation.** The Windows UI lists paired devices and can revoke one or
   all. Revocation takes effect immediately. A lost phone is revoked, not merely
   hidden.
7. **Replay protection.** Include a nonce, timestamp, method, path, body hash,
   device ID, and monotonic request ID in the signed envelope. Reject stale
   timestamps and reused nonces.
8. **Rate and size limits.** Per-device request rate, maximum body size, maximum
   task length, maximum concurrent tasks, and a bounded queue. Return `429`-style
   local errors without side effects.

There is no default pairing token, default password, or "allow all" mode.

## API shape

The bridge should be a thin adapter over `Assistant`/`TaskEngine`, not a second
assistant:

| Endpoint (illustrative) | Purpose | Notes |
|---|---|---|
| `GET /v1/status` | Version, offline mode, kill switch, capability summary | No secrets; authenticated |
| `GET /v1/catalog` | Registered tools and risk levels | Redacted schema only |
| `POST /v1/tasks` | Submit one typed command | Bounded text; returns task ID; idempotency key required |
| `GET /v1/tasks/{id}` | State, step summaries, verified flags | Only the submitting device may read |
| `POST /v1/tasks/{id}/cancel` | Cooperative cancel | Owner/device scoped |
| `POST /v1/tasks/{id}/approve` | Optional future remote approval | Disabled by default; separate opt-in and local code |
| `GET /v1/audit/tail` | Recent structured audit events | Optional, redacted, bounded; may be omitted |

The bridge must use a strict JSON parser, reject unknown fields where practical,
validate lengths, and return errors without echoing secrets. It must not accept
raw tool names/arguments from the phone unless that is a separately reviewed
privileged feature; the normal path is a natural-language command through the
same deterministic parser.

Voice audio upload is not part of this design. If a future phone sends
transcribed text, it is still untrusted input. Sending raw audio would require a
separate privacy review and a local transcription path.

## Approval and authorization

- Read-only status may be allowed after pairing.
- Reversible actions may be allowed only if the local policy already
  auto-approves them; the bridge must not lower the threshold.
- Sensitive/privileged actions require local desktop approval by default.
- If remote approval is ever supported, the phone must display the same tool,
  risk, args preview, reason, and undoability as the desktop; approval must be
  bound to the exact task/step and expire. A stolen unlocked phone must not be
  enough to force-kill an app or delete memory.
- The Windows kill switch always wins. The bridge cannot resume it remotely.
- Tasks from the bridge must carry an origin field into the audit log and memory.

## Threat model

| Threat | Required mitigation |
|---|---|
| Anyone on the LAN reaches an open port | Loopback/USB by default; private interface + firewall + mTLS if LAN is used |
| Stolen phone | Device key in Keystore, short sessions, revocation, local approval for high risk, screen lock |
| Malicious app on the phone | OS app sandbox, no exported intents with secrets, certificate pinning, no WebView bridge |
| MITM on Wi-Fi | TLS with pinning/mTLS; no plaintext fallback |
| Replay of a captured request | Nonce + timestamp + request ID + body hash; reject duplicates |
| Token theft from Windows | Store only hashes; protect with DPAPI/ACL; rotate/revoke |
| DNS rebinding / browser CSRF | No browser-based API; require client cert/signature; validate Origin/Host if HTTP is used; never rely on a cookie |
| Malicious task text | Same parser/policy/approval; no direct tool execution; bounded input |
| Data exfiltration | No cloud relay; redacted audit; no arbitrary file reads; path policy; task output limits |
| Denial of service | Rate/size/concurrency limits; timeouts; kill switch; bounded queue |
| Unauthorized device discovery | Explicit pairing; mDNS off by default; no device names in unauthenticated responses |

The bridge does not protect against a compromised Windows account or a
compromised Android OS. It reduces remote exposure; it is not a replacement for
device encryption, screen locks, or OS updates.

## Configuration proposal

The existing reserved block is a starting point:

```yaml
integration:
  android_enabled: false          # must remain false until implemented
  bind_host: "127.0.0.1"          # loopback only by default
  bind_port: 8765
  require_pairing_token: true     # must not be disabled in a supported build
```

A future implementation would add, with validation:

```yaml
integration:
  android_enabled: false
  transport: usb_adb              # usb_adb | private_tls
  bind_host: "127.0.0.1"
  bind_port: 8765
  require_pairing_token: true
  tls_cert: "<APP_ROOT>\\data\\android\\bridge.crt"
  tls_key: "<APP_ROOT>\\data\\android\\bridge.key"   # protected; never logged
  paired_devices_file: "<APP_ROOT>\\data\\android\\devices.json"
  pairing_window_seconds: 120
  max_body_bytes: 65536
  max_concurrent_tasks: 2
  remote_approval_enabled: false
  allowed_remote_tools: ["system.info", "system.battery", "windows.network_status"]
```

Validation must reject `android_enabled: true` when:
- the bridge would bind a non-loopback address without TLS and pairing;
- `require_pairing_token` is false;
- no certificate/key path is configured for a TLS transport;
- remote approval is enabled without a separate local confirmation mechanism;
- the requested tool set includes tools outside a reviewed allowlist.

## Implementation plan (not started)

1. Define the protocol and threat model in tests before code.
2. Implement a loopback-only bridge with no authentication bypass; add a
   development-only fake client in tests.
3. Add pairing, device key storage, revocation, and signed request envelopes.
4. Wire requests to `Assistant.handle_command`/`TaskEngine`; do not duplicate
   policy.
5. Add local approval and remote cancel tests, including replay, expired
   certificates, unknown devices, oversized bodies, and kill switch.
6. Add audit events and a visible connection indicator.
7. Only then consider private-LAN TLS; keep USB as the recommended path.
8. Record an acceptance run with two physical devices and a network capture
   showing no unintended egress.

## Acceptance criteria

An Android milestone is not complete until:

- starting the app with defaults opens no remote port;
- an unpaired client receives no data and cannot submit a task;
- a paired client can submit a bounded typed command and see honest
  verified/unverified states;
- sensitive actions still require local approval by default;
- revocation immediately blocks the device;
- replay, stale timestamps, oversized bodies, and malformed JSON are denied;
- kill switch stops new remote work and cancels active tasks;
- a packet capture confirms the documented transport only;
- tests and docs state the exact environment and limitations.

Do not claim Android support before these checks are actually run. The current
repository intentionally contains no bridge code.
