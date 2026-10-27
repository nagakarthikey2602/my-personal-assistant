"""Allowlisted developer commands and deterministic log analysis."""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_assistant.errors import ValidationError


def test_dev_status_lists_only_reviewed_commands(tool) -> None:
    call, _assistant = tool
    result = call("dev.status")
    assert result.status == "ok"
    commands = result.data["commands"]
    assert set(commands) == {"pytest", "unittest", "compileall", "git_status", "git_log", "git_diff"}
    assert all(item["enabled"] for item in commands.values())
    assert "python -m pytest -q" in commands["pytest"]["command"]


def test_analyze_log_is_pattern_based_and_safe(tool, workspace: Path) -> None:
    call, _assistant = tool
    log = workspace / "sandbox" / "build.log"
    log.write_text(
        "starting build\n"
        "warning: unused import\n"
        "ERROR: missing dependency package-x\n"
        "Traceback (most recent call last):\n"
        "  File \"main.py\", line 4\n"
        "Permission denied while opening config\n",
        encoding="utf-8",
    )
    result = call("dev.analyze_log", path=str(log), max_lines=20)
    assert result.status == "ok", result.message
    assert result.data["matches"]
    assert any("missing dependency" in item["text"].lower() for item in result.data["matches"])
    assert any("traceback" in item["text"].lower() for item in result.data["matches"])
    assert "deterministic" in result.message
    assert "untrusted" in result.data["note"].lower()

    clean = workspace / "sandbox" / "clean.log"
    clean.write_text("all checks completed\nBUILD SUCCESS\n", encoding="utf-8")
    no_matches = call("dev.analyze_log", path=str(clean))
    assert no_matches.status == "ok"
    assert no_matches.data["matches"] == []
    assert "No common failure markers" in no_matches.message


def test_dev_run_rejects_unknown_command_before_execution(tool, workspace: Path) -> None:
    call, _assistant = tool
    with pytest.raises(ValidationError):
        call("dev.run", project=str(workspace / "sandbox"), command="shell")


def test_dev_run_requires_a_write_root(tool, workspace: Path) -> None:
    call, _assistant = tool
    result = call("dev.run", project=str(workspace), command="compileall")
    assert result.status == "error"
    assert "Write access" in result.message


def test_dev_run_respects_disabled_terminal(make_assistant, workspace: Path) -> None:
    assistant, _approver = make_assistant(overrides={"tools": {"terminal_enabled": False}})
    token = assistant.safety.new_token("dev_disabled")
    try:
        result = assistant.executor.execute(
            "dev.run",
            {"project": str(workspace / "sandbox"), "command": "compileall"},
            token=token,
            task_id="dev_disabled",
        )
    finally:
        assistant.safety.release_token(token)
    assert result.status == "error"
    assert "disabled" in result.message.lower()


def test_dev_run_rejects_command_removed_from_allowlist(make_assistant, workspace: Path) -> None:
    assistant, _approver = make_assistant(overrides={"tools": {"allowed_commands": []}})
    token = assistant.safety.new_token("dev_allowlist")
    try:
        result = assistant.executor.execute(
            "dev.run",
            {"project": str(workspace / "sandbox"), "command": "compileall"},
            token=token,
            task_id="dev_allowlist",
        )
    finally:
        assistant.safety.release_token(token)
    assert result.status == "error"
    assert "allowed_commands" in result.message


def test_dev_run_executes_reviewed_compile_command(tool, workspace: Path) -> None:
    call, _assistant = tool
    sandbox = workspace / "sandbox"
    (sandbox / "sample.py").write_text("value = 1 + 1\n", encoding="utf-8")

    result = call("dev.run", project=str(sandbox), command="compileall", timeout_seconds=60)
    assert result.status == "ok", result.message
    assert result.data["exit_code"] == 0
    assert result.data["reviewed_output_only"] is True
    assert Path(result.data["project"]) == sandbox.resolve()
    assert (sandbox / "__pycache__").exists()
