"""Assistant orchestration: parse, plan, approve, execute, verify, and report.

This module is the only high-level entry point for commands. Typed and voice
commands take the same path. Every PC action goes through the tool executor.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from ..audit import AuditLogger
from ..config import Config
from ..errors import AssistantError, PermissionDenied, ToolUnavailable, ValidationError
from ..memory.store import MemoryStore
from ..offline_guard import install_network_guard
from ..speech.service import SpeechService
from ..util import redact, safe_preview
from .context import ToolContext
from .executor import ToolExecutor
from .parser import CommandParser, ParseResult
from .paths import PathPolicy
from .permissions import ApprovalCallback, ApprovalGrant, PermissionPolicy
from .planner import ExecutionPlan, TaskPlanner
from .recovery import RecoveryJournal
from .registry import RiskLevel, ToolRegistry
from .results import ToolResult
from .safety import SafetyController
from .task import StepState, TaskEngine, TaskOutcome, TaskState

logger = logging.getLogger(__name__)


@dataclass
class AssistantResponse:
    text: str
    kind: str  # task | builtin | clarify | error
    spoken_text: str = ""
    outcome: TaskOutcome | None = None
    parse: ParseResult | None = None
    details: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        if self.kind in {"builtin", "clarify"}:
            return True
        return bool(self.outcome and self.outcome.ok)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "kind": self.kind,
            "spoken_text": self.spoken_text,
            "ok": self.ok,
            "outcome": self.outcome.to_dict() if self.outcome else None,
            "parse": {
                "kind": self.parse.kind,
                "intent": self.parse.intent,
                "source": self.parse.source,
                "confidence": self.parse.confidence,
            } if self.parse else None,
            "details": self.details,
        }


class Assistant:
    LIMITATIONS = [
        "Speech recognition can use user-recorded templates, an optional local Vosk model, or an optional local Whisper model; none sends audio off this PC.",
        "Wake-word listening is opt-in and keeps the microphone open while it is enabled.",
        "Formant speech output is robotic and has not been perceptually validated.",
        "Browser support opens local pages or explicit URLs; it does not fill forms or automate arbitrary pages.",
        "UI automation can launch/focus/close apps and type text, but it has no accessibility-tree verification.",
        "Windows settings pages can be opened; the assistant does not change OS settings or registry startup entries.",
        "Screenshots capture all screens and are stored locally.",
        "Undo covers supported filesystem operations only, not app/browser/typing actions.",
        "The intent model is a small locally trained closed-label classifier; it never executes actions.",
        "The application network guard covers Python sockets only, not child processes or native libraries.",
        "No cloud AI, telemetry, or remote control server is used. The optional Vosk and Whisper ASR models run locally.",
    ]

    def __init__(
        self,
        config: Config,
        *,
        approval_callback: ApprovalCallback | None = None,
        install_guard: bool = True,
    ) -> None:
        self.config = config
        self.config.ensure_directories()
        self.config.seed_bundled_models()
        if install_guard and config.get("app.offline_mode") == "strict":
            install_network_guard()
        self.audit = AuditLogger(config)
        self.path_policy = PathPolicy(config)
        self.safety = SafetyController()
        self.recovery = RecoveryJournal(config, self.path_policy)
        self.memory = MemoryStore(config)
        self.registry: ToolRegistry = self._build_registry()
        self.policy = PermissionPolicy(config, approval_callback)
        self.executor = ToolExecutor(
            config=config,
            registry=self.registry,
            policy=self.policy,
            path_policy=self.path_policy,
            audit=self.audit,
            safety=self.safety,
            recovery=self.recovery,
            memory=self.memory,
        )
        self.parser = CommandParser(config)
        self.planner = TaskPlanner(self.registry, config)
        self.tasks = TaskEngine(
            config=config,
            executor=self.executor,
            policy=self.policy,
            safety=self.safety,
            recovery=self.recovery,
            memory=self.memory,
        )
        self._speech_lock = threading.Lock()
        self._speech: SpeechService | None = None
        self._last_outcome: TaskOutcome | None = None
        self.audit.log(
            "assistant_started",
            offline_mode=config.get("app.offline_mode"),
            tools=len(self.registry.names()),
            model_available=self.parser.model is not None,
            memory_enabled=bool(config.get("memory.enabled")),
        )

    def _build_registry(self) -> ToolRegistry:
        from ..tools import build_registry

        return build_registry(self.config)

    # Speech ---------------------------------------------------------------
    def speech_service(self) -> SpeechService:
        with self._speech_lock:
            if self._speech is None:
                self._speech = SpeechService(self.config)
            return self._speech

    def speak(self, text: str) -> tuple[bool, str]:
        text = text.strip()
        if not text:
            return False, "There was nothing to speak."
        try:
            self.speech_service().speak(text[:2000])
        except Exception as exc:
            logger.warning("Speech output failed: %s", exc.__class__.__name__)
            return False, f"Speech output failed: {exc}"
        return True, "Spoken locally."

    def reload_speech(self) -> None:
        """Rebuild the speech service after settings change, stopping old audio."""
        with self._speech_lock:
            if self._speech is not None:
                try:
                    self._speech.stop_all()
                except Exception:
                    logger.exception("Could not stop the previous speech service")
            self._speech = None

    def listen(self, max_seconds: float | None = None) -> dict[str, Any]:
        try:
            transcription = self.speech_service().record_and_transcribe(max_seconds)
        except Exception as exc:
            logger.exception("Voice capture failed")
            return {"available": False, "text": "", "reason": f"Voice capture failed: {exc}"}
        return transcription.to_dict()

    def listen_and_handle(
        self,
        max_seconds: float | None = None,
        *,
        speak: bool = False,
    ) -> AssistantResponse:
        result = self.listen(max_seconds=max_seconds)
        if not result.get("available"):
            return AssistantResponse(
                text=result.get("reason") or "Voice input is unavailable.",
                kind="error",
                spoken_text="Voice input is unavailable.",
            )
        text = str(result.get("text") or "").strip()
        if not text:
            return AssistantResponse(
                text=result.get("reason") or "I did not catch a command. Please repeat or type it.",
                kind="clarify",
            )
        return self.handle_voice_command(
            text,
            speak=speak,
            confidence=float(result.get("confidence") or 0.0),
            engine=str(result.get("engine") or ""),
        )

    def handle_voice_command(
        self,
        text: str,
        *,
        speak: bool = False,
        confidence: float = 0.0,
        engine: str = "vosk-offline",
    ) -> AssistantResponse:
        """Execute an already-transcribed voice command through the normal path."""
        raw = str(text or "").strip()
        if not raw:
            return AssistantResponse(
                text="I did not catch a command. Please repeat or type it.",
                kind="clarify",
            )
        self.audit.log(
            "voice_transcribed",
            characters=len(raw),
            confidence=round(float(confidence), 4),
            engine=engine,
        )
        response = self.handle_command(raw, speak=False)
        response.details.insert(0, f"Voice transcript: {raw}")
        if speak:
            self._speak_if_configured(response.spoken_text or response.text)
        return response

    def _speak_if_configured(self, text: str) -> None:
        if not self.config.get("app.ui.spoken_responses", True):
            return
        if not self.config.get("app.ui.voice_enabled", True):
            return
        if str(self.config.get("speech.tts_engine", "formant")) == "none":
            return
        ok, _message = self.speak(text)
        if not ok:
            logger.info("Response was not spoken because the TTS backend was unavailable.")

    # Main command path ----------------------------------------------------
    def handle_command(self, text: str, *, speak: bool = False) -> AssistantResponse:
        raw = str(text or "").strip()
        if not raw:
            return AssistantResponse(
                text="Please type or say a command.",
                kind="clarify",
                spoken_text="Please type or say a command.",
            )
        if len(raw) > 4000:
            return AssistantResponse(
                text="That command is too long. Please keep a single request under 4,000 characters.",
                kind="error",
            )

        self.audit.log("command_received", characters=len(raw), kill_switch=self.safety.halted)

        # A kill switch must not be silently reset by a model or parser result.
        if self.safety.halted and _norm(raw) not in {
            "resume", "reset kill switch", "deactivate kill switch", "clear kill switch"
        }:
            response = AssistantResponse(
                text="The kill switch is active, so no action will run. Say or type 'resume' to reset it.",
                kind="error",
                spoken_text="The kill switch is active. No action will run.",
            )
            return self._finish(response, speak)

        parsed = self.parser.parse(raw)
        self.audit.log(
            "command_parsed",
            kind=parsed.kind,
            intent=parsed.intent[:120],
            source=parsed.source,
            confidence=round(parsed.confidence, 4),
            steps=len(parsed.steps),
        )

        if parsed.kind == "clarify":
            response = AssistantResponse(
                text=parsed.message or "I did not understand that command. Nothing was run.",
                kind="clarify",
                spoken_text=parsed.message or "I did not understand that command. Nothing was run.",
                parse=parsed,
            )
            self.audit.log("command_clarification", intent=parsed.intent[:120], source=parsed.source)
            return self._finish(response, speak)

        if parsed.kind == "builtin":
            response = self._run_builtin(parsed)
            return self._finish(response, speak)

        if parsed.kind != "plan" or not parsed.steps:
            response = AssistantResponse(
                text="I could not build a safe action plan. Nothing was run.",
                kind="error",
                parse=parsed,
            )
            return self._finish(response, speak)

        try:
            plan = self.planner.build(parsed)
        except (ValidationError, ToolUnavailable) as exc:
            response = AssistantResponse(
                text=f"I could not prepare that action: {exc} Nothing was run.",
                kind="error",
                parse=parsed,
            )
            self.audit.log("plan_rejected", reason=str(exc)[:500], intent=parsed.intent[:120])
            return self._finish(response, speak)
        except AssistantError as exc:
            response = AssistantResponse(text=str(exc), kind="error", parse=parsed)
            return self._finish(response, speak)

        self.audit.log(
            "plan_ready",
            task_intent=plan.intent[:120],
            steps=len(plan.steps),
            highest_risk=plan.highest_risk,
            source=plan.source,
            tools=[step.tool for step in plan.steps],
        )
        try:
            outcome = self.tasks.execute(plan, command=raw)
        except Exception as exc:
            logger.exception("Task execution failed before a normal outcome")
            self.audit.log("task_exception", reason=exc.__class__.__name__)
            response = AssistantResponse(
                text=f"The task could not run: {exc.__class__.__name__}. Details are in the local log.",
                kind="error",
                parse=parsed,
            )
            return self._finish(response, speak)

        self._last_outcome = outcome
        response = self._format_outcome(outcome, parsed)
        return self._finish(response, speak)

    def _finish(self, response: AssistantResponse, speak: bool) -> AssistantResponse:
        if speak:
            self._speak_if_configured(response.spoken_text or response.text)
        return response

    def cancel(self) -> int:
        count = self.safety.cancel_all("Stopped by the user.")
        if self._speech is not None:
            self._speech.stop_all()
        self.audit.log("user_cancelled_tasks", count=count)
        return count

    def halt(self, reason: str = "Kill switch activated by the user.") -> None:
        self.safety.halt(reason)
        if self._speech is not None:
            self._speech.stop_all()
        self.audit.log("kill_switch_activated", reason=reason)

    def resume(self) -> None:
        self.safety.resume()
        self.audit.log("kill_switch_reset")

    def shutdown(self) -> None:
        self.safety.cancel_all("The assistant is shutting down.")
        if self._speech is not None:
            self._speech.stop_all()
        self.audit.log("assistant_stopped")

    # Builtins -------------------------------------------------------------
    def _run_builtin(self, parsed: ParseResult) -> AssistantResponse:
        name = parsed.intent
        if name == "builtin.help":
            text = self.help_text()
            return AssistantResponse(text=text, kind="builtin", parse=parsed,
                                     spoken_text="I have listed the supported commands in the window.")
        if name == "builtin.capabilities":
            text = self.capabilities_text()
            return AssistantResponse(text=text, kind="builtin", parse=parsed,
                                     spoken_text="I have listed my capabilities and limitations in the window.")
        if name == "builtin.status":
            data = self.status()
            text = self._format_status(data)
            return AssistantResponse(text=text, kind="builtin", parse=parsed,
                                     spoken_text="The assistant is running. Status details are in the window.")
        if name == "builtin.halt":
            self.halt()
            return AssistantResponse(
                text="Kill switch activated. All running tasks were asked to cancel and no new action will run until you resume.",
                kind="builtin", parse=parsed,
                spoken_text="Kill switch activated. No action will run until you resume.",
            )
        if name == "builtin.resume":
            self.resume()
            return AssistantResponse(
                text="Kill switch reset. Actions are allowed again, subject to the normal permission policy.",
                kind="builtin", parse=parsed,
                spoken_text="Kill switch reset.",
            )
        if name == "builtin.stop":
            count = self.cancel()
            return AssistantResponse(
                text=f"Stop requested. {count} active task token(s) were cancelled.",
                kind="builtin", parse=parsed,
                spoken_text="Stopped.",
            )
        if name == "builtin.exit":
            return AssistantResponse(
                text="Close the assistant window to exit. Typing 'exit' only ends an interactive text session.",
                kind="builtin", parse=parsed,
                spoken_text="Close the window to exit.",
            )
        if name == "builtin.listen":
            return AssistantResponse(
                text=(
                    "Use the Push to talk button in the window to record a short command, or run the app with "
                    "--voice for a console push-to-talk loop. Enable the wake-word listener to say 'hearty' "
                    "followed by a command. The listener is opt-in and shows its status in the window."
                ),
                kind="builtin", parse=parsed,
                spoken_text="Use push to talk, or enable the hearty wake word in the window.",
            )
        return AssistantResponse(
            text=f"The built-in command '{name}' is not implemented. Nothing was run.",
            kind="error", parse=parsed,
        )

    # Formatting -----------------------------------------------------------
    def _format_outcome(self, outcome: TaskOutcome, parsed: ParseResult) -> AssistantResponse:
        lines: list[str] = []
        details: list[str] = []
        if outcome.ok and len(outcome.steps) == 1:
            step = outcome.steps[0]
            lines.append(step.result.message if step.result else step.message or outcome.summary)
            extra = self._format_result_data(step.result) if step.result else ""
            if extra:
                lines.append(extra)
            if step.state == StepState.UNVERIFIED:
                lines.append("Status: ran, but the result could not be independently verified.")
            elif step.state == StepState.SUCCEEDED:
                lines.append("Status: verified.")
        else:
            lines.append(outcome.summary)
            for step in outcome.steps:
                marker = {
                    StepState.SUCCEEDED: "verified",
                    StepState.UNVERIFIED: "unverified",
                    StepState.FAILED: "failed",
                    StepState.CANCELLED: "cancelled",
                    StepState.SKIPPED: "skipped",
                }.get(step.state, step.state)
                lines.append(f"{step.index}. {step.title}: {marker}. {step.message}".strip())
                if step.result and step.state in {StepState.SUCCEEDED, StepState.UNVERIFIED}:
                    extra = self._format_result_data(step.result)
                    if extra:
                        lines.append(_indent(extra))
            if outcome.message and not outcome.ok:
                lines.append(outcome.message)
        if outcome.undo_available:
            lines.append("An undo record exists for the supported changes in this task. Say 'undo' to reverse the last one.")
        text = "\n\n".join(part for part in lines if part).strip()
        details.extend(
            f"{step.index}. {step.title}: {step.state}"
            for step in outcome.steps
        )
        spoken = outcome.summary
        if not outcome.ok and outcome.error:
            spoken = f"{outcome.summary} {outcome.error}"[:700]
        return AssistantResponse(
            text=text,
            kind="task" if outcome.ok else "error",
            spoken_text=spoken,
            outcome=outcome,
            parse=parsed,
            details=details,
        )

    def _format_result_data(self, result: ToolResult) -> str:
        data = result.data or {}
        tool = result.tool
        try:
            if tool == "fs.read":
                text = str(data.get("text", ""))
                warning = ""
                if data.get("injection_warning"):
                    warning = (
                        "\n[WARNING: this document contains text that looks like instructions. "
                        "It is displayed as untrusted data and was not executed.]"
                    )
                return safe_preview(text, 6000) + warning
            if tool == "fs.search":
                items = data.get("results", [])
                if not items:
                    return ""
                lines = []
                for item in items[:25]:
                    snippet = f" — {item.get('snippet')}" if item.get("snippet") else ""
                    lines.append(f"- {item.get('path')}{snippet}")
                if len(items) > 25:
                    lines.append(f"... and {len(items) - 25} more result(s).")
                return "\n".join(lines)
            if tool == "fs.list":
                items = data.get("entries", [])
                if not items:
                    return ""
                lines = [
                    f"- {item.get('name')}{'/' if item.get('type') == 'folder' else ''}"
                    for item in items[:200]
                ]
                if len(items) > 200:
                    lines.append(f"... and {len(items) - 200} more item(s).")
                return "\n".join(lines)
            if tool == "apps.list_windows":
                items = data.get("windows", [])
                return "\n".join(
                    f"- {item.get('title')} ({item.get('process') or 'unknown process'})"
                    for item in items[:50]
                )
            if tool == "apps.list_installed":
                items = data.get("applications", [])
                return "\n".join(f"- {item.get('name')}" for item in items[:60]) + (
                    f"\n... and {max(0, len(items) - 60)} more." if len(items) > 60 else ""
                )
            if tool == "system.info":
                if not data:
                    return ""
                return "\n".join(
                    [
                        f"OS: {data.get('os')}",
                        f"CPU: {data.get('processor')} ({data.get('logical_cpus')} logical cores)",
                        f"Memory: {_bytes(data.get('memory_available'))} available of {_bytes(data.get('memory_total'))}",
                        f"Disk free: {_bytes(data.get('disk_free'))}",
                        f"Python: {data.get('python')}",
                        f"Offline mode: {data.get('offline_mode')}",
                    ]
                )
            if tool == "system.top_processes":
                items = data.get("processes", [])
                return "\n".join(
                    f"- {item.get('name')} (pid {item.get('pid')}): "
                    f"{_bytes(item.get('memory_bytes'))}, CPU {item.get('cpu_percent')}%"
                    for item in items[:20]
                )
            if tool == "windows.network_status":
                interfaces = data.get("interfaces", [])
                lines = [
                    f"- {item.get('name')}: {'up' if item.get('up') else 'down'}"
                    for item in interfaces
                ]
                wifi = data.get("wifi") or {}
                if wifi:
                    lines.append("Wi-Fi: " + ", ".join(f"{key}={value}" for key, value in wifi.items()))
                return "\n".join(lines)
            if tool == "windows.startup_list":
                return "\n".join(
                    f"- {item.get('name')}: {safe_preview(str(item.get('command', '')), 160)}"
                    for item in data.get("entries", [])[:60]
                )
            if tool == "desktop.clipboard_get":
                return safe_preview(str(data.get("text", "")), 3000)
            if tool == "memory.list":
                preferences = data.get("preferences", [])
                tasks = data.get("recent_tasks", [])
                lines = [
                    f"- {item.get('key')} = {safe_preview(str(item.get('value')), 200)}"
                    for item in preferences[:50]
                ]
                if tasks:
                    lines.append(f"Recent tasks stored: {len(tasks)}")
                return "\n".join(lines)
            if tool == "memory.status":
                return "\n".join(
                    f"{key}: {value}" for key, value in data.items()
                    if key in {"enabled", "tasks", "preferences", "training_examples", "retention_days", "database"}
                )
            if tool in {"fs.duplicates"}:
                groups = data.get("groups", [])
                lines = [
                    f"- {group.get('count')} copies, {_bytes(group.get('size'))} each: "
                    + ", ".join(group.get("paths", [])[:3])
                    for group in groups[:20]
                ]
                return "\n".join(lines)
            if tool == "dev.run":
                output = data.get("output", "")
                if output:
                    return safe_preview(str(output), 5000)
                return ""
            if tool == "dev.analyze_log":
                return "\n".join(
                    f"{item.get('line')}: {item.get('text')}"
                    for item in data.get("matches", [])[:60]
                )
            if tool == "voice.list_devices":
                lines = [
                    f"- input {item.get('index')}: {item.get('name')}"
                    for item in data.get("inputs", [])
                ]
                lines += [
                    f"- output {item.get('index')}: {item.get('name')}"
                    for item in data.get("outputs", [])
                ]
                return "\n".join(lines)
            if tool == "voice.status":
                return (
                    f"Microphones: {len(data.get('audio', {}).get('inputs', []))}; "
                    f"speakers: {len(data.get('audio', {}).get('outputs', []))}; "
                    f"ASR available: {bool(data.get('asr', {}).get('available'))}."
                )
            if tool in {"fs.write", "fs.copy", "fs.move", "fs.rename", "fs.zip", "fs.extract",
                        "desktop.screenshot", "memory.export", "fs.inventory"}:
                path = data.get("path") or data.get("destination") or data.get("archive") or data.get("output")
                return f"Location: {path}" if path else ""
            if tool == "fs.undo_last":
                return f"Undid: {data.get('description')}."
            if tool == "system.offline_status":
                return (
                    f"Mode: {data.get('mode')}; guard installed: {data.get('guard', {}).get('installed')}; "
                    f"blocked attempts: {data.get('guard', {}).get('blocked_attempts')}."
                )
            if tool == "windows.display_info":
                return result.message
            return ""
        except Exception:
            logger.exception("Could not format result data for %s", tool)
            return ""

    # Public status/capability data ---------------------------------------
    def help_text(self) -> str:
        categories = {
            "files": "search/read/list, write .txt/.md/.docx, create folders, copy/move/rename, quarantine delete, zip/extract, duplicates, inventory, undo",
            "apps": "launch installed apps, list/focus/close windows, type Unicode text, list installed applications",
            "windows": "display/network/Bluetooth/power/startup status; open Settings pages and folders",
            "system": "system info, top processes, battery, audio devices, media keys, offline status",
            "desktop": "clipboard read/write, full-screen screenshot",
            "developer": "fixed allowlisted pytest/unittest/compileall/Git commands, log error scan",
            "memory": "remember/list/export/delete preferences, enable or disable memory, add training examples",
            "voice": "status, device list, local speech output; push-to-talk is in the window",
            "browser": "open local file:// pages; external URLs only if explicitly enabled outside strict mode",
        }
        lines = ["Supported command groups:"]
        lines.extend(f"- {name}: {description}" for name, description in categories.items())
        lines.extend(
            [
                "",
                "Examples:",
                "- create a file notes.txt in sandbox with content Hello",
                "- find pdf files in downloads",
                "- copy notes.txt to downloads",
                "- take a screenshot",
                "- open notepad",
                "- type \"hello world\" in notepad",
                "- remember that my editor is notepad",
                "- undo",
                "",
                "Safety: high-impact actions ask for approval. Destructive file actions quarantine instead of permanently deleting. "
                "Say 'capabilities' for the full honest limitation list.",
            ]
        )
        return "\n".join(lines)

    def capabilities_text(self) -> str:
        lines = ["Implemented capabilities:"]
        grouped: dict[str, list[str]] = {}
        for spec in self.registry.all():
            grouped.setdefault(spec.category, []).append(
                f"{spec.name} ({RiskLevel.label(spec.risk)})"
            )
        for category in sorted(grouped):
            lines.append(f"- {category}: " + ", ".join(grouped[category]))
        lines.append("")
        lines.append("Limitations and honest gaps:")
        lines.extend(f"- {item}" for item in self.LIMITATIONS)
        model = self.parser.model_status()
        lines.append("")
        lines.append(
            "Local intent model: "
            + ("loaded; label suggestion only" if model.get("available") else "not available; deterministic rules only")
        )
        return "\n".join(lines)

    def status(self) -> dict[str, Any]:
        speech: dict[str, Any]
        try:
            speech = self.speech_service().status()
        except Exception as exc:
            speech = {"error": f"{exc.__class__.__name__}: {exc}", "available": False}
        recent = []
        try:
            recent = self.memory.recent_tasks(3)
        except Exception:
            pass
        return {
            "configuration": str(self.config.source or "built-in defaults"),
            "offline_mode": self.config.get("app.offline_mode"),
            "safety": self.safety.status(),
            "memory": self.memory.status(),
            "recovery": self.recovery.status(),
            "model": self.parser.model_status(),
            "speech": {
                "asr": speech.get("asr", {}),
                "tts": speech.get("tts", {}),
                "audio_input_available": speech.get("audio", {}).get("input_available"),
                "audio_output_available": speech.get("audio", {}).get("output_available"),
                "wake_word": speech.get("wake_word", {}),
                "limitations": speech.get("limitations", []),
            },
            "tools": len(self.registry.names()),
            "recent_tasks": recent,
            "last_task": self._last_outcome.to_dict() if self._last_outcome else None,
        }

    def _format_status(self, data: dict[str, Any]) -> str:
        safety = data.get("safety", {})
        memory = data.get("memory", {})
        model = data.get("model", {})
        speech = data.get("speech", {})
        recovery = data.get("recovery", {})
        lines = [
            f"Offline mode: {data.get('offline_mode')}",
            f"Kill switch: {'ACTIVE' if safety.get('halted') else 'off'}",
            f"Tools registered: {data.get('tools')}",
            f"Memory: {'enabled' if memory.get('enabled') else 'disabled'}"
            + (f" ({memory.get('tasks', 0)} tasks, {memory.get('preferences', 0)} preferences)" if memory.get("enabled") else ""),
            f"Intent model: {'loaded' if model.get('available') else 'not loaded'}"
            + (" — label suggestion only" if model.get("available") else f" ({model.get('reason', '')})"),
            f"Speech input available: {speech.get('audio_input_available')}; speech output available: "
            f"{speech.get('tts', {}).get('available')} ({speech.get('tts', {}).get('configured_engine')})",
            f"Wake word: {speech.get('wake_word', {}).get('word', 'hearty')} "
            f"({'enabled' if speech.get('wake_word', {}).get('enabled') else 'disabled'})",
            f"Undo records: {recovery.get('entries', 0)} ({recovery.get('pending', 0)} pending)",
            f"Configuration: {data.get('configuration')}",
        ]
        if data.get("recent_tasks"):
            lines.append("Recent tasks:")
            for task in data["recent_tasks"]:
                lines.append(f"- {task.get('created_at')} {task.get('intent')}: {task.get('state')}")
        return "\n".join(lines)

    # UI helpers -----------------------------------------------------------
    def catalog(self) -> list[dict[str, Any]]:
        return self.registry.catalog()

    def approval_bridge_ready(self) -> bool:
        return self.policy.approval_callback is not None

    def memory_export(self, path: str) -> AssistantResponse:
        """Convenience method for the UI. Still goes through the tool executor."""
        return self.handle_command(f"export memory to {path}")


def _indent(text: str, prefix: str = "   ") -> str:
    return "\n".join(prefix + line if line else line for line in str(text).splitlines())


def _bytes(value: Any) -> str:
    try:
        from ..util import format_bytes

        return format_bytes(int(value))
    except (TypeError, ValueError):
        return "unknown"


def _norm(value: str) -> str:
    return " ".join(value.lower().strip().rstrip(".!?").split())
