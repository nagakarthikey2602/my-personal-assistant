"""End-to-end typed commands through parser, planner, permissions, and tools."""

from __future__ import annotations

import json
from pathlib import Path

from personal_assistant.config import Config


def _assistant(make_assistant, workspace: Path, *, approve: bool = True):
    return make_assistant(
        approve=approve,
        overrides={"paths": {"read_roots": [str(workspace / "sandbox")]}},
    )


def test_full_file_lifecycle_through_natural_language(make_assistant, workspace: Path) -> None:
    assistant, approver = _assistant(make_assistant, workspace)
    sandbox = workspace / "sandbox"

    created = assistant.handle_command(
        "create a file integration.txt in sandbox with content hello integration"
    )
    assert created.ok, created.text
    assert (sandbox / "integration.txt").read_text(encoding="utf-8") == "hello integration"

    read = assistant.handle_command("read integration.txt")
    assert read.ok and "hello integration" in read.text

    listed = assistant.handle_command("list sandbox")
    assert listed.ok and "integration.txt" in listed.text

    searched = assistant.handle_command("search sandbox for integration")
    assert searched.ok and "integration.txt" in searched.text

    copied = assistant.handle_command("copy integration.txt to integration-copy.txt")
    assert copied.ok, copied.text
    assert (sandbox / "integration-copy.txt").exists()

    renamed = assistant.handle_command("rename integration-copy.txt to integration-renamed.txt")
    assert renamed.ok, renamed.text
    assert (sandbox / "integration-renamed.txt").exists()

    deleted = assistant.handle_command("delete integration-renamed.txt")
    assert deleted.ok, deleted.text
    assert not (sandbox / "integration-renamed.txt").exists()
    assert approver.requests and approver.requests[-1].risk == 3

    undone = assistant.handle_command("undo")
    assert undone.ok, undone.text
    assert (sandbox / "integration-renamed.txt").exists()


def test_composite_plan_executes_in_order(make_assistant, workspace: Path) -> None:
    assistant, _ = _assistant(make_assistant, workspace)
    sandbox = workspace / "sandbox"

    response = assistant.handle_command(
        "create a file chain.txt in sandbox with content chained then read chain.txt"
    )
    assert response.ok, response.text
    assert response.outcome is not None
    assert len(response.outcome.steps) == 2
    assert [step.tool for step in response.outcome.steps] == ["fs.write", "fs.read"]
    assert all(step.state == "succeeded" for step in response.outcome.steps)
    assert (sandbox / "chain.txt").read_text(encoding="utf-8") == "chained"


def test_memory_commands_and_export(make_assistant, workspace: Path) -> None:
    assistant, _ = _assistant(make_assistant, workspace)

    remembered = assistant.handle_command("remember that my editor is notepad")
    assert remembered.ok, remembered.text
    assert assistant.memory.get_preference("editor") == "notepad"

    listed = assistant.handle_command("what do you remember")
    assert listed.ok
    assert "editor" in listed.text and "notepad" in listed.text

    exported = assistant.handle_command("export memory to memory.json")
    assert exported.ok, exported.text
    report = workspace / "sandbox" / "memory.json"
    assert report.exists()
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert any(item["key"] == "editor" for item in payload["preferences"])

    forgotten = assistant.handle_command("forget editor")
    assert forgotten.ok, forgotten.text
    assert assistant.memory.get_preference("editor") is None

    disabled = assistant.handle_command("disable memory")
    assert disabled.ok, disabled.text
    assert assistant.memory.status()["enabled"] is False
    blocked = assistant.handle_command("remember that my editor is vim")
    assert not blocked.ok
    enabled = assistant.handle_command("enable memory")
    assert enabled.ok, enabled.text
    assert assistant.memory.status()["enabled"] is True


def test_builtin_help_status_and_capabilities(make_assistant, workspace: Path) -> None:
    assistant, _ = _assistant(make_assistant, workspace)

    help_response = assistant.handle_command("help")
    assert help_response.kind == "builtin" and help_response.ok
    assert "Supported command groups" in help_response.text

    status = assistant.handle_command("status")
    assert status.kind == "builtin" and status.ok
    assert "Offline mode: strict" in status.text
    assert "Kill switch: off" in status.text

    capabilities = assistant.handle_command("capabilities")
    assert capabilities.kind == "builtin" and capabilities.ok
    assert "local Vosk model" in capabilities.text
    assert "Wake-word listening is opt-in" in capabilities.text
    assert "No cloud AI" in capabilities.text


def test_voice_transcript_uses_the_normal_command_path(make_assistant, workspace: Path) -> None:
    assistant, _ = _assistant(make_assistant, workspace)

    response = assistant.handle_voice_command(
        "status",
        confidence=0.91,
        engine="vosk-offline",
    )

    assert response.ok
    assert "Offline mode: strict" in response.text
    assert response.details[0] == "Voice transcript: status"

    empty = assistant.handle_voice_command("   ")
    assert empty.kind == "clarify"
    assert empty.outcome is None


def test_unknown_command_runs_nothing(make_assistant, workspace: Path) -> None:
    assistant, _ = _assistant(make_assistant, workspace)
    response = assistant.handle_command("please make me a sandwich using every file")
    assert response.kind == "clarify"
    assert response.outcome is None
    assert "Nothing was run" in response.text or "did not understand" in response.text
    assert not any((workspace / "sandbox").iterdir())


def test_path_escape_is_denied_and_nothing_is_written(make_assistant, workspace: Path) -> None:
    assistant, _ = _assistant(make_assistant, workspace)
    target = workspace / "outside.txt"

    response = assistant.handle_command(f"create a file {target} with content escaped")
    assert not response.ok
    assert not target.exists()
    assert "Write access" in response.text or "denied" in response.text.lower()

    read = assistant.handle_command(f"read {workspace / 'config' / 'config.yaml'}")
    assert not read.ok
    assert "private internal data" in read.text or "denied" in read.text.lower()


def test_kill_switch_command_and_resume(make_assistant, workspace: Path) -> None:
    assistant, _ = _assistant(make_assistant, workspace)
    activated = assistant.handle_command("kill switch")
    assert activated.ok and assistant.safety.halted

    blocked = assistant.handle_command("create a file no.txt in sandbox with content x")
    assert blocked.kind == "error" and "kill switch" in blocked.text.lower()
    assert not (workspace / "sandbox" / "no.txt").exists()

    assistant.handle_command("resume")
    allowed = assistant.handle_command("create a file yes.txt in sandbox with content x")
    assert allowed.ok
    assert (workspace / "sandbox" / "yes.txt").exists()


def test_clarification_does_not_trigger_approval(make_assistant, workspace: Path) -> None:
    assistant, approver = _assistant(make_assistant, workspace)
    for command in ("create a file", "copy something", "read", "delete"):
        response = assistant.handle_command(command)
        assert response.kind == "clarify", command
        assert response.outcome is None
    assert approver.requests == []


def test_read_model_still_never_supplies_arguments(make_assistant, workspace: Path) -> None:
    """A model suggestion can select a handler but deterministic rules fill the args."""
    assistant, _ = _assistant(make_assistant, workspace)
    parsed = assistant.parser._parse_single("create a file argcheck.txt in sandbox with content fixed")
    assert parsed.kind == "plan"
    assert parsed.steps[0].args["content"] == "fixed"
    assert parsed.steps[0].args["path"].endswith("argcheck.txt")
