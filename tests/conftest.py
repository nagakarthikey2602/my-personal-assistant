"""Shared fixtures for the local assistant test suite.

Every test runs inside a temporary PA_HOME. No test reads or writes the
project's runtime data, records from a microphone, plays audio, changes the
clipboard, launches applications, or captures the screen unless that specific
test is explicitly marked and run by the owner of the machine.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

import pytest

# NumPy on Windows can fail during OpenBLAS initialisation when many test
# processes load it at once. The application uses a small model; one thread is
# faster and more reliable.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

from personal_assistant.config import Config  # noqa: E402
from personal_assistant.core.assistant import Assistant  # noqa: E402
from personal_assistant.core.permissions import AutoApprover  # noqa: E402


def build_test_config(root: Path, **overrides: Any) -> Config:
    overlay: dict[str, Any] = {
        "app": {"log_level": "WARNING"},
        "paths": {"write_roots": [str(root / "sandbox")]},
        "intelligence": {"enable_model": False},
        "speech": {"asr_engine": "none", "tts_engine": "none"},
        "memory": {"enabled": True, "store_commands": True},
    }
    for section, values in overrides.items():
        if isinstance(values, dict) and isinstance(overlay.get(section), dict):
            overlay[section] = {**overlay[section], **values}
        else:
            overlay[section] = values
    return Config.from_dict(overlay, root)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """An isolated PA_HOME with an empty sandbox."""
    (tmp_path / "sandbox").mkdir()
    return tmp_path


@pytest.fixture
def config(workspace: Path) -> Config:
    cfg = build_test_config(workspace)
    cfg.ensure_directories()
    return cfg


@pytest.fixture
def make_assistant(
    workspace: Path,
) -> Callable[..., tuple[Assistant, AutoApprover]]:
    """Build assistants with a recording auto-approver and clean shutdown."""

    created: list[Assistant] = []

    def factory(
        *,
        approve: bool = True,
        overrides: dict[str, Any] | None = None,
        cfg: Config | None = None,
    ) -> tuple[Assistant, AutoApprover]:
        approver = AutoApprover(approve)
        assistant_config = cfg or build_test_config(workspace, **(overrides or {}))
        assistant = Assistant(
            assistant_config,
            approval_callback=approver,
            install_guard=False,
        )
        created.append(assistant)
        return assistant, approver

    yield factory

    for assistant in created:
        try:
            assistant.shutdown()
        except Exception:
            pass


@pytest.fixture
def tool_factory(make_assistant):
    """Call registered tools directly through the real executor and permission gate."""
    assistant, approver = make_assistant()
    token = assistant.safety.new_token("test_task")

    def call(tool_name: str, **args: Any):
        return assistant.executor.execute(tool_name, args, token=token, task_id="test_task")

    yield call, assistant
    assistant.safety.release_token(token)


@pytest.fixture
def tool(tool_factory):
    """Convenience alias for tool_factory's ``(call, assistant)`` pair."""
    return tool_factory


@pytest.fixture
def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def require_windows() -> None:
    if os.name != "nt":
        pytest.skip("This check requires Windows.")


@pytest.fixture
def no_python_network_guard():
    """Ensure the global socket guard is uninstalled around a test."""
    from personal_assistant.offline_guard import uninstall_network_guard

    uninstall_network_guard()
    try:
        yield
    finally:
        uninstall_network_guard()


