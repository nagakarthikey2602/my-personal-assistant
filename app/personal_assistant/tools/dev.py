"""Allowlisted developer commands and deterministic build-log analysis."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import psutil

from ..core.context import ToolContext
from ..core.registry import RiskLevel, ToolSpec, object_schema
from ..core.results import ToolResult
from ..util import safe_preview

CREATE_NO_WINDOW = 0x08000000

_COMMANDS: dict[str, list[str]] = {
    "pytest": ["python", "-m", "pytest", "-q"],
    "unittest": ["python", "-m", "unittest", "discover", "-v"],
    "compileall": ["python", "-m", "compileall", "-q", "."],
    "git_status": ["git", "status", "--short"],
    "git_log": ["git", "log", "-n", "20", "--oneline"],
    "git_diff": ["git", "diff", "--stat"],
}


def register(registry) -> None:  # type: ignore[no-untyped-def]
    registry.register(ToolSpec(
        name="dev.run",
        description="Run one of the reviewed test, compile, or Git commands in an allowed project folder.",
        risk=RiskLevel.SENSITIVE,
        category="developer",
        schema=object_schema({
            "project": {"type": "string", "minLength": 1, "maxLength": 30000},
            "command": {"type": "string", "enum": sorted(_COMMANDS.keys())},
            "timeout_seconds": {"type": "integer", "minimum": 5, "maximum": 300, "default": 120},
        }, required=["project", "command"]),
        handler=_run,
    ))
    registry.register(ToolSpec(
        name="dev.analyze_log",
        description="Extract failing lines and context from a local build, test, or error log.",
        risk=RiskLevel.READ_ONLY,
        category="developer",
        schema=object_schema({
            "path": {"type": "string", "minLength": 1, "maxLength": 30000},
            "max_lines": {"type": "integer", "minimum": 5, "maximum": 200, "default": 60},
        }, required=["path"]),
        handler=_analyze_log,
    ))
    registry.register(ToolSpec(
        name="dev.status",
        description="Show the configured developer command allowlist and tool availability.",
        risk=RiskLevel.READ_ONLY,
        category="developer",
        schema=object_schema({}),
        handler=_status,
    ))


def _configured_allowed(ctx: ToolContext) -> list[list[str]]:
    raw = ctx.config.get("tools.allowed_commands", [])
    if not isinstance(raw, list):
        return []
    return [[str(item) for item in command] for command in raw if isinstance(command, list)]


def _resolve_command(ctx: ToolContext, kind: str) -> tuple[list[str] | None, str]:
    if not ctx.config.get("tools.terminal_enabled", True):
        return None, "Developer commands are disabled in configuration."
    canonical = _COMMANDS[kind]
    allowed = _configured_allowed(ctx)
    if canonical not in allowed:
        return None, (
            f"The command for '{kind}' is no longer in tools.allowed_commands. "
            "Add the exact reviewed command to configuration to enable it."
        )
    resolved = list(canonical)
    if resolved and resolved[0] == "python":
        resolved[0] = sys.executable
    return resolved, ""


def _kill_tree(process: subprocess.Popen[Any]) -> None:
    try:
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.terminate()
            except psutil.Error:
                pass
        try:
            parent.terminate()
        except psutil.Error:
            pass
        psutil.wait_procs(children + [parent], timeout=3)
        for child in children + [parent]:
            try:
                if child.is_running():
                    child.kill()
            except psutil.Error:
                pass
    except (psutil.Error, OSError):
        try:
            process.kill()
        except OSError:
            pass


def _run(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    project = ctx.path_policy.check_write(args["project"])
    if not project.is_dir():
        return ToolResult.error("dev.run", "The project folder does not exist.")
    kind = args["command"]
    command, reason = _resolve_command(ctx, kind)
    if command is None:
        return ToolResult.error("dev.run", reason)
    if kind in {"pytest", "unittest"} and not (project / "tests").exists():
        # Not fatal: a project may use a different layout; the command output will explain.
        pass
    timeout = min(int(args.get("timeout_seconds", 120)), int(ctx.config.get("tools.terminal_timeout_seconds", 120)))
    temp_dir = ctx.config.path("paths.temp")
    temp_dir.mkdir(parents=True, exist_ok=True)
    log_path = temp_dir / f"devrun_{uuid.uuid4().hex[:10]}.log"
    ctx.check_cancel()
    started = time.monotonic()
    env = os.environ.copy()
    env["PA_OFFLINE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        with open(log_path, "w+b") as output:
            process = subprocess.Popen(
                command,
                cwd=str(project),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                shell=False,
                env=env,
                creationflags=CREATE_NO_WINDOW,
            )
            while True:
                if ctx.cancelled:
                    _kill_tree(process)
                    return ToolResult.cancelled("dev.run", "Developer command cancelled.")
                if time.monotonic() - started > timeout:
                    _kill_tree(process)
                    return ToolResult.error(
                        "dev.run",
                        f"Command exceeded the {timeout}s timeout and its process tree was terminated.",
                    )
                if process.poll() is not None:
                    break
                time.sleep(0.2)
            output.seek(0)
            raw = output.read()
        text = raw.decode("utf-8", errors="replace")
        max_output = int(ctx.config.get("tools.terminal_max_output_bytes", 200_000))
        truncated = len(text.encode("utf-8", "replace")) > max_output
        if truncated:
            text = text[-max_output:]
            text = "...[earlier output truncated]\n" + text
        summary_lines = _summary_lines(text)
        summary = summary_lines[-1] if summary_lines else f"Exit code {process.returncode}."
        data = {
            "command": " ".join(command),
            "project": str(project),
            "exit_code": process.returncode,
            "duration_seconds": round(time.monotonic() - started, 2),
            "summary": summary,
            "output": safe_preview(text, 30000),
            "truncated": truncated,
            "reviewed_output_only": True,
        }
        if process.returncode == 0:
            return ToolResult.success(
                "dev.run",
                f"Command completed successfully: {summary}",
                data=data,
            )
        return ToolResult.error("dev.run", f"Command failed (exit {process.returncode}): {summary}", data=data)
    except FileNotFoundError:
        return ToolResult.error("dev.run", "The configured executable was not found on this computer.")
    except OSError as exc:
        return ToolResult.error("dev.run", f"Could not run the command: {exc.__class__.__name__}: {exc}")
    finally:
        try:
            log_path.unlink(missing_ok=True)
        except OSError:
            pass


def _summary_lines(text: str) -> list[str]:
    patterns = [
        r"(?i)\b\d+\s+passed\b.*",
        r"(?i)\b\d+\s+failed\b.*",
        r"(?i)\b(?:ok|FAILED|ERROR)\b.*",
        r"(?i)\bBUILD (?:FAILED|SUCCESS)\b.*",
        r"(?i)\bTraceback \(most recent call last\)",
    ]
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if any(re.search(pattern, stripped) for pattern in patterns):
            lines.append(stripped[:500])
    return lines[-20:]


def _analyze_log(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = ctx.path_policy.check_read(args["path"])
    if not path.is_file():
        return ToolResult.error("dev.analyze_log", "The log file does not exist.")
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ToolResult.error("dev.analyze_log", f"Could not read the log: {exc.__class__.__name__}")
    lines = raw.splitlines()
    if len(lines) > 200000:
        lines = lines[-200000:]
    markers = re.compile(
        r"(?i)(\berror\b|\bfailed\b|\bfailure\b|\bexception\b|traceback|not found|cannot find|"
        r"permission denied|access denied|syntaxerror|typeerror|importerror|build failed)"
    )
    selected: list[dict[str, Any]] = []
    seen: set[int] = set()
    for index, line in enumerate(lines):
        if markers.search(line):
            for offset in range(-1, 2):
                position = index + offset
                if 0 <= position < len(lines) and position not in seen:
                    seen.add(position)
                    selected.append({"line": position + 1, "text": lines[position][:500]})
    selected = selected[: int(args.get("max_lines", 60))]
    if not selected:
        return ToolResult.success(
            "dev.analyze_log",
            "No common failure markers were found. The assistant cannot infer more without a model.",
            data={"path": str(path), "matches": [], "lines_scanned": len(lines)},
        )
    return ToolResult.success(
        "dev.analyze_log",
        f"Found {len(selected)} relevant log line(s). These are deterministic pattern matches, not a causal diagnosis.",
        data={
            "path": str(path),
            "matches": selected,
            "lines_scanned": len(lines),
            "note": "Untrusted log content is displayed as data and never executed.",
        },
    )


def _status(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    available = {}
    for kind, canonical in _COMMANDS.items():
        _, reason = _resolve_command(ctx, kind)
        available[kind] = {"command": " ".join(canonical), "enabled": not bool(reason), "reason": reason}
    return ToolResult.success(
        "dev.status",
        f"{sum(1 for item in available.values() if item['enabled'])} of {len(available)} developer commands are enabled.",
        data={"commands": available, "working_write_roots": [str(path) for path in ctx.config.write_roots()]},
    )
