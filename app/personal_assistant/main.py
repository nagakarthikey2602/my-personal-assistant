"""Application entry point: desktop GUI, CLI, diagnostics, and self-tests."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import logging
import os
import platform
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# The intent model is tiny. A single BLAS/OpenMP worker is faster and avoids
# OpenBLAS allocation failures seen when several Python processes start at once
# on constrained Windows sessions. An explicit user choice is respected.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

from . import __version__
from .config import Config, load_config
from .core.assistant import Assistant
from .core.permissions import ApprovalRequest
from .logging_setup import setup_logging
from .offline_guard import (
    install_network_guard,
    is_installed,
    local_only_policy_note,
    self_test_local_socket,
    status as guard_status,
)

logger = logging.getLogger(__name__)

PACKAGE_NAMES = ("numpy", "psutil", "PyYAML", "pypdf", "Pillow")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="personal-assistant",
        description="Offline-first local Windows personal assistant.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", help="Path to a YAML configuration file.")
    parser.add_argument("--cli", action="store_true", help="Run the interactive text console instead of the GUI.")
    parser.add_argument("--voice", action="store_true", help="Run a push-to-talk voice loop in the console.")
    parser.add_argument("--command", help="Run one command and exit.")
    parser.add_argument("--speak", action="store_true", help="Speak the one-shot response when TTS is enabled.")
    parser.add_argument("--diagnose", action="store_true", help="Check the local installation and exit.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable diagnostics.")
    parser.add_argument("--self-test", action="store_true", help="Run safe built-in functional checks in a temp sandbox.")
    parser.add_argument("--offline-check", action="store_true", help="Verify the local socket guard and loopback access.")
    parser.add_argument("--list-tools", action="store_true", help="List registered tools and exit.")
    parser.add_argument("--verbose", action="store_true", help="Print step details in the console.")
    return parser


def _cli_approval(request: ApprovalRequest) -> bool:
    print("\nAPPROVAL REQUIRED")
    print(f"  Tool: {request.tool} ({request.risk_label})")
    print(f"  What: {request.title}")
    print(f"  Args: {request.args_preview or '(none)'}")
    print(f"  Why:  {request.reason}")
    if not request.undoable:
        print("  Note: this action is not covered by the filesystem undo journal.")
    if not sys.stdin.isatty():
        print("  No interactive terminal is available, so the action is denied.")
        return False
    try:
        answer = input("  Allow this action? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in {"y", "yes"}


def _print_response(response: Any, verbose: bool = False, speak: bool = False) -> None:
    print()
    print(response.text)
    if verbose and response.details:
        print("\nDetails:")
        for item in response.details:
            print(f"  - {item}")
    if response.outcome:
        print(f"\nTask state: {response.outcome.state} (verified: {response.outcome.verified})")
    if speak and response.spoken_text:
        # The assistant has already spoken it when requested; this is a caption.
        print(f"Spoken: {response.spoken_text}")


def _cli(assistant: Assistant, config: Config, verbose: bool = False) -> int:
    print(f"Local Personal Assistant {__version__} — offline mode: {config.get('app.offline_mode')}")
    print(f"{len(assistant.registry.names())} tools registered. Type 'help' for examples, 'exit' to quit.")
    print("High-impact actions ask for explicit approval. Destructive file actions quarantine; 'undo' reverses supported changes.")
    while True:
        try:
            text = input("\nassistant> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text.lower() in {"exit", "quit", ":q"}:
            break
        response = assistant.handle_command(text, speak=False)
        _print_response(response, verbose=verbose)
    assistant.shutdown()
    print("Assistant stopped.")
    return 0


def _voice_loop(assistant: Assistant, config: Config) -> int:
    print(f"Voice push-to-talk console — offline mode: {config.get('app.offline_mode')}")
    print("This uses only phrases you recorded with scripts/record_voice_templates.py. Ctrl+C exits.")
    status = assistant.speech_service().status()
    if not status.get("asr", {}).get("available"):
        reason = status.get("asr", {}).get("reason") or "No voice templates are installed."
        print(f"Voice recognition is unavailable: {reason}")
        print("Type commands with --cli instead, or record templates first.")
        assistant.shutdown()
        return 2
    while True:
        try:
            input("\nPress Enter, then speak your command...")
            response = assistant.listen_and_handle(speak=True)
        except (EOFError, KeyboardInterrupt):
            print()
            break
        except Exception as exc:
            print(f"Voice loop error: {exc.__class__.__name__}: {exc}")
            continue
        print()
        print(response.text)
        if response.outcome:
            print(f"\nTask state: {response.outcome.state} (verified: {response.outcome.verified})")
    assistant.shutdown()
    print("Assistant stopped.")
    return 0


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "not installed"


def _check(name: str, ok: bool, detail: str, severity: str = "error") -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail, "severity": severity}


def diagnose(config: Config) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    checks.append(_check("platform", os.name == "nt", f"{platform.system()} {platform.release()} {platform.version()}"))
    checks.append(
        _check(
            "python",
            sys.version_info >= (3, 11),
            f"{platform.python_version()} at {sys.executable}",
        )
    )
    for package in PACKAGE_NAMES:
        version = _package_version(package)
        checks.append(_check(f"dependency:{package}", version != "not installed", version))
    tk_ok, tk_detail = _tkinter_available()
    checks.append(_check("tkinter", tk_ok, tk_detail))

    try:
        directories = config.ensure_directories()
        seeded = config.seed_bundled_models()
        detail = f"{len(directories)} runtime directories ensured under {config.root}"
        if seeded:
            detail += f"; seeded {len(seeded)} packaged model file(s)"
        checks.append(_check("directories", True, detail))
    except OSError as exc:
        checks.append(_check("directories", False, f"{exc.__class__.__name__}: {exc}"))
        directories = []

    roots = config.write_roots()
    if roots:
        try:
            roots[0].mkdir(parents=True, exist_ok=True)
            probe = roots[0] / f".write_probe_{os.getpid()}"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            checks.append(_check("sandbox-write", True, str(roots[0])))
        except OSError as exc:
            checks.append(_check("sandbox-write", False, f"{exc.__class__.__name__}: {exc}"))
    else:
        checks.append(_check("sandbox-write", False, "No write root is configured."))

    schema = Path(__file__).resolve().parent / "memory" / "schema.sql"
    checks.append(_check("package-data:memory-schema", schema.exists(), str(schema)))
    sapi = Path(__file__).resolve().parent / "speech" / "sapi_tts.ps1"
    checks.append(_check("package-data:sapi-script", sapi.exists(), str(sapi), severity="warning"))

    try:
        from .speech.native_audio import audio_status

        audio = audio_status()
        checks.append(
            _check(
                "audio-devices",
                bool(audio.get("input_available") and audio.get("output_available")),
                f"inputs={len(audio.get('inputs', []))}, outputs={len(audio.get('outputs', []))}",
                severity="warning",
            )
        )
    except Exception as exc:
        checks.append(_check("audio-devices", False, f"{exc.__class__.__name__}: {exc}", severity="warning"))

    try:
        from .core.parser import CommandParser

        parser_status = CommandParser(config).model_status()
        checks.append(
            _check(
                "intent-model",
                bool(parser_status.get("available")),
                str(parser_status.get("path") or parser_status.get("reason") or ""),
                severity="warning",
            )
        )
    except Exception as exc:
        checks.append(_check("intent-model", False, f"{exc.__class__.__name__}: {exc}", severity="warning"))

    try:
        from .speech.service import SpeechService

        speech = SpeechService(config).status()
        asr = speech.get("asr", {})
        checks.append(
            _check(
                "voice-templates",
                bool(asr.get("available")),
                str(asr.get("reason") or f"{asr.get('phrase_count', 0)} phrase(s), {asr.get('take_count', 0)} take(s)"),
                severity="warning",
            )
        )
    except Exception as exc:
        checks.append(_check("voice-templates", False, f"{exc.__class__.__name__}: {exc}", severity="warning"))

    try:
        from .tools import build_registry

        registry = build_registry(config)
        checks.append(_check("tool-registry", len(registry.names()) >= 40, f"{len(registry.names())} tools registered"))
    except Exception as exc:
        checks.append(_check("tool-registry", False, f"{exc.__class__.__name__}: {exc}"))

    mode = config.get("app.offline_mode")
    valid_mode = mode in {"strict", "advisory"}
    if valid_mode and mode == "strict":
        # Make the diagnostic prove that the guard can actually be installed in
        # this interpreter. This process exits after the report; it does not
        # change any persistent system setting.
        install_network_guard()
    checks.append(
        _check(
            "offline-mode",
            valid_mode and (mode != "strict" or is_installed()),
            f"{mode} (guard installed: {is_installed()})",
        )
    )
    try:
        loop_ok, loop_detail = self_test_local_socket()
        checks.append(_check("loopback-sockets", loop_ok, loop_detail, severity="warning"))
    except Exception as exc:
        checks.append(_check("loopback-sockets", False, f"{exc.__class__.__name__}: {exc}", severity="warning"))

    errors = [check for check in checks if not check["ok"] and check["severity"] == "error"]
    warnings = [check for check in checks if not check["ok"] and check["severity"] != "error"]
    return {
        "version": __version__,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config_source": str(config.source or "built-in defaults"),
        "config_root": str(config.root),
        "data_path": config.get("paths.data"),
        "checks": checks,
        "ok": not errors,
        "errors": len(errors),
        "warnings": len(warnings),
        "note": local_only_policy_note(),
    }


def _tkinter_available() -> tuple[bool, str]:
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        root.update_idletasks()
        root.destroy()
        return True, f"Tk {tkinter.TkVersion}"
    except Exception as exc:
        return False, f"{exc.__class__.__name__}: {exc}"


def _run_diagnostics(config: Config, as_json: bool) -> int:
    report = diagnose(config)
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"Local Personal Assistant {report['version']} diagnostics")
        print(f"Config: {report['config_source']}")
        print(f"Root:   {report['config_root']}")
        for check in report["checks"]:
            if check["ok"]:
                marker = "OK  "
            elif check["severity"] == "error":
                marker = "FAIL"
            else:
                marker = "WARN"
            print(f"  [{marker}] {check['name']}: {check['detail']}")
        print(f"\nResult: {'healthy' if report['ok'] else 'problems found'} ({report['errors']} error(s), {report['warnings']} warning(s))")
        print(report["note"])
    return 0 if report["ok"] else 1


def _run_offline_check(config: Config, as_json: bool) -> int:
    from .errors import NetworkBlockedError

    install_network_guard()
    blocked = False
    detail = ""
    try:
        socket.create_connection(("192.0.2.1", 9), timeout=1.0)
        detail = "External connection unexpectedly succeeded."
    except NetworkBlockedError as exc:
        blocked = True
        detail = str(exc)
    except OSError as exc:
        detail = f"External connection failed with OS error instead of the guard: {exc}"
    loop_ok, loop_detail = self_test_local_socket()
    report = {
        "offline_mode": config.get("app.offline_mode"),
        "guard": guard_status(),
        "external_blocked": blocked,
        "external_detail": detail,
        "loopback_ok": loop_ok,
        "loopback_detail": loop_detail,
        "policy_note": local_only_policy_note(),
    }
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("Offline guard check")
        print(f"  Mode:              {report['offline_mode']}")
        print(f"  Guard installed:   {report['guard']['installed']}")
        print(f"  External blocked:  {blocked}")
        print(f"  Detail:            {detail}")
        print(f"  Loopback sockets:  {loop_ok} ({loop_detail})")
        print(f"  {report['policy_note']}")
    return 0 if blocked and loop_ok else 1


def _run_self_test(verbose: bool = False) -> int:
    from .core.permissions import AutoApprover

    root = Path(tempfile.mkdtemp(prefix="pa-selftest-"))
    failures: list[str] = []
    try:
        config = Config.from_dict(
            {
                "app": {"log_level": "WARNING"},
                "paths": {"write_roots": [str(root / "sandbox")]},
                "intelligence": {"enable_model": False},
                "speech": {"asr_engine": "none", "tts_engine": "none"},
                "memory": {"enabled": True, "store_commands": True},
            },
            root,
        )
        config.ensure_directories()
        approver = AutoApprover(True)
        assistant = Assistant(config, approval_callback=approver, install_guard=False)
        checks: list[tuple[str, bool, str]] = []

        def run(command: str) -> Any:
            response = assistant.handle_command(command, speak=False)
            if verbose:
                print(f"  > {command}\n    {response.kind}: {response.text.splitlines()[0] if response.text else ''}")
            return response

        response = run("create a file selftest.txt in sandbox with content hello self test")
        checks.append(("write+verify", bool(response.outcome and response.outcome.verified), response.text[:160]))
        response = run("read selftest.txt")
        text_ok = bool(response.outcome and "hello self test" in (response.outcome.steps[0].result.data.get("text", "") if response.outcome.steps and response.outcome.steps[0].result else ""))
        checks.append(("read+verify", bool(response.outcome and response.outcome.verified) and text_ok, response.text[:160]))
        response = run("copy selftest.txt to sandbox\\selftest-copy.txt")
        checks.append(("copy+verify", bool(response.outcome and response.outcome.verified), response.text[:160]))
        response = run("rename selftest-copy.txt to selftest-renamed.txt")
        checks.append(("rename+verify", bool(response.outcome and response.outcome.verified), response.text[:160]))
        response = run("delete selftest-renamed.txt")
        checks.append(("quarantine-delete", bool(response.outcome and response.outcome.verified), response.text[:160]))
        response = run("undo")
        checks.append(("undo", bool(response.outcome and response.outcome.verified), response.text[:160]))
        response = run("remember that my self test key is self test value")
        checks.append(("memory-write", bool(response.outcome and response.outcome.verified), response.text[:160]))
        response = run("what do you remember")
        checks.append(("memory-read", bool(response.outcome and response.outcome.verified), response.text[:160]))
        response = run("status")
        checks.append(("status", response.kind == "builtin" and response.ok, response.text.splitlines()[0] if response.text else ""))
        response = run("this sentence should not run anything")
        checks.append(("unknown-command-safe", response.kind == "clarify" and not response.outcome, response.text[:160]))
        checks.append(("approval-bridge", bool(approver.requests), f"{len(approver.requests)} approval(s) requested"))
        checks.append(("kill-switch", not assistant.safety.halted, "initially off"))
        assistant.halt()
        checks.append(("kill-switch-activate", assistant.safety.halted, "halted"))
        blocked = run("take a screenshot")
        checks.append(("kill-switch-blocks", blocked.kind == "error" and not blocked.outcome, "action blocked"))
        assistant.resume()
        checks.append(("kill-switch-reset", not assistant.safety.halted, "resumed"))
        assistant.shutdown()

        for name, ok, detail in checks:
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
            if not ok:
                failures.append(name)
        print(f"\nSelf-test: {len(checks) - len(failures)}/{len(checks)} passed")
    except Exception as exc:
        logger.exception("Self-test crashed")
        print(f"Self-test crashed: {exc.__class__.__name__}: {exc}")
        failures.append("crash")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return 0 if not failures else 1


def _run_one_shot(assistant: Assistant, command: str, speak: bool, verbose: bool) -> int:
    response = assistant.handle_command(command, speak=speak)
    _print_response(response, verbose=verbose, speak=speak)
    assistant.shutdown()
    return 0 if response.ok or response.kind == "clarify" else 1


def _list_tools(config: Config) -> int:
    from .tools import build_registry

    registry = build_registry(config)
    for spec in registry.all():
        print(f"{spec.name:32s} risk {spec.risk} ({spec.category:10s})  {spec.description}")
    print(f"\n{len(registry.names())} tools registered.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(path=args.config)
    except Exception as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.diagnose:
        return _run_diagnostics(config, args.json)
    if args.offline_check:
        return _run_offline_check(config, args.json)
    if args.self_test:
        return _run_self_test(args.verbose)
    if args.list_tools:
        return _list_tools(config)

    try:
        config.ensure_directories()
    except OSError as exc:
        print(f"Could not create runtime directories: {exc}", file=sys.stderr)
        return 2

    if args.cli or args.command or args.voice:
        setup_logging(config, console=True)
    else:
        setup_logging(config, console=False)

    assistant = Assistant(config, approval_callback=_cli_approval)
    if args.command:
        return _run_one_shot(assistant, args.command, args.speak, args.verbose)
    if args.voice:
        return _voice_loop(assistant, config)
    if args.cli:
        return _cli(assistant, config, args.verbose)

    try:
        from .ui.app import AssistantWindow
        from .core.permissions import ApprovalBridge
    except Exception as exc:
        print(f"The desktop interface could not be loaded: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        print("Use --cli for the text console.", file=sys.stderr)
        assistant.shutdown()
        return 2
    bridge = ApprovalBridge(timeout_seconds=float(config.get("permissions.approval_timeout_seconds", 120)))
    assistant.policy.approval_callback = bridge.ask
    window = AssistantWindow(assistant, bridge, config)
    try:
        window.run()
    finally:
        # shutdown is idempotent enough for normal close paths
        try:
            assistant.shutdown()
        except Exception:
            logger.exception("Final shutdown failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
