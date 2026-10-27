"""Subprocess checks for the installed-command-line surface.

Each child process receives its own temporary PA_HOME so it cannot read or
write the owner's real assistant data.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _env(home: Path, project_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PA_HOME"] = str(home)
    env.pop("PA_CONFIG", None)
    env["PYTHONPATH"] = str(project_root / "app") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["OPENBLAS_NUM_THREADS"] = "1"
    env["OMP_NUM_THREADS"] = "1"
    return env


def _run(project_root: Path, home: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "personal_assistant", *args],
        cwd=project_root,
        env=_env(home, project_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


@pytest.mark.integration
def test_version_flag(project_root: Path, tmp_path: Path) -> None:
    result = _run(project_root, tmp_path / "home", "--version", timeout=30)
    assert result.returncode == 0, result.stderr
    assert "personal-assistant 0.1.0" in result.stdout


@pytest.mark.integration
def test_list_tools_does_not_create_runtime_data(project_root: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = _run(project_root, home, "--list-tools", timeout=60)
    assert result.returncode == 0, result.stderr
    assert "fs.write" in result.stdout
    assert "risk 1" in result.stdout or "risk 2" in result.stdout
    assert "tools registered." in result.stdout
    assert not (home / "data" / "assistant.db").exists()


@pytest.mark.integration
def test_one_shot_command_uses_isolated_sandbox(project_root: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = _run(
        project_root,
        home,
        "--command",
        "create a file cli.txt in sandbox with content hello from the CLI",
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Local Personal Assistant" not in result.stderr
    target = home / "sandbox" / "cli.txt"
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "hello from the CLI"


@pytest.mark.integration
@pytest.mark.offline
def test_offline_check_child_process(project_root: Path, tmp_path: Path) -> None:
    result = _run(project_root, tmp_path / "home", "--offline-check", "--json", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["external_blocked"] is True
    assert report["loopback_ok"] is True
    assert report["guard"]["installed"] is True
    assert "child processes" in report["policy_note"]
