"""Recovery journal semantics and failure handling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from personal_assistant.core.recovery import RecoveryJournal
from personal_assistant.errors import VerificationError


def test_no_entries_means_nothing_to_undo(make_assistant) -> None:
    assistant, _ = make_assistant()
    with pytest.raises(VerificationError, match="no supported operation"):
        assistant.recovery.undo_last()


def test_latest_non_undoable_operation_blocks_older_undo(make_assistant, workspace: Path) -> None:
    assistant, _ = make_assistant()
    target = workspace / "sandbox" / "one.txt"
    target.write_text("one", encoding="utf-8")
    assistant.recovery.record(
        description="first",
        tool="test",
        task_id="t",
        actions=[{"op": "quarantine_path", "path": str(target)}],
    )
    assistant.recovery.record(
        description="not undoable",
        tool="test",
        task_id="t",
        actions=[],
        undoable=False,
    )
    with pytest.raises(VerificationError, match="not undoable"):
        assistant.recovery.undo_last()
    assert target.exists()


def test_undo_refuses_changed_file(make_assistant, workspace: Path) -> None:
    assistant, _ = make_assistant()
    target = workspace / "sandbox" / "changed.txt"
    target.write_text("original", encoding="utf-8")
    from personal_assistant.util import sha256_file

    assistant.recovery.record(
        description="remove",
        tool="test",
        task_id="t",
        actions=[{"op": "remove_file", "path": str(target), "sha256": sha256_file(target)}],
    )
    target.write_text("modified", encoding="utf-8")
    with pytest.raises(VerificationError, match="changed"):
        assistant.recovery.undo_last()
    assert target.read_text(encoding="utf-8") == "modified"


def test_undo_refuses_to_remove_non_empty_created_directory(make_assistant, workspace: Path) -> None:
    assistant, _ = make_assistant()
    folder = workspace / "sandbox" / "created"
    folder.mkdir()
    (folder / "unexpected.txt").write_text("do not delete", encoding="utf-8")
    assistant.recovery.record(
        description="create folder",
        tool="test",
        task_id="t",
        actions=[{"op": "remove_dir", "path": str(folder)}],
    )
    with pytest.raises(VerificationError, match="not empty"):
        assistant.recovery.undo_last()
    assert folder.exists()
    assert (folder / "unexpected.txt").exists()


def test_undo_rejects_paths_outside_policy(make_assistant, workspace: Path) -> None:
    assistant, _ = make_assistant()
    outside = workspace / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    assistant.recovery.record(
        description="escape",
        tool="test",
        task_id="t",
        actions=[{"op": "quarantine_path", "path": str(outside)}],
    )
    with pytest.raises(VerificationError, match="Write access"):
        assistant.recovery.undo_last()
    assert outside.exists()


def test_journal_persists_and_reports_status(make_assistant, workspace: Path) -> None:
    assistant, _ = make_assistant()
    target = workspace / "sandbox" / "persisted.txt"
    target.write_text("x", encoding="utf-8")
    undo_id = assistant.recovery.record(
        description="persist",
        tool="test",
        task_id="t",
        actions=[{"op": "quarantine_path", "path": str(target)}],
    )
    path = assistant.recovery.path
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["entries"][0]["id"] == undo_id

    reloaded = RecoveryJournal(assistant.config, assistant.path_policy)
    assert reloaded.list_entries()[0]["id"] == undo_id
    status = reloaded.status()
    assert status["entries"] == 1
    assert status["pending"] == 1
    assert status["last"]["description"] == "persist"


def test_unknown_undo_action_is_refused(make_assistant) -> None:
    assistant, _ = make_assistant()
    assistant.recovery.record(
        description="unknown",
        tool="test",
        task_id="t",
        actions=[{"op": "launch_missiles"}],
    )
    with pytest.raises(VerificationError, match="Unsupported undo action"):
        assistant.recovery.undo_last()
