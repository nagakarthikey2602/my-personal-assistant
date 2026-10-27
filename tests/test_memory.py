"""SQLite memory: preferences, task history, retention, export, and deletion."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from personal_assistant.config import Config
from personal_assistant.memory.store import MemoryStore


@pytest.fixture
def store(config: Config) -> MemoryStore:
    return MemoryStore(config)


def test_preferences_round_trip_and_delete(store: MemoryStore) -> None:
    store.set_preference("editor", "notepad")
    store.set_preference("theme", {"mode": "dark", "accent": "blue"})
    assert store.get_preference("editor") == "notepad"
    assert store.get_preference("theme") == {"mode": "dark", "accent": "blue"}
    assert store.get_preference("missing") is None

    names = {item["key"] for item in store.list_preferences()}
    assert {"editor", "theme"} <= names
    assert store.delete_preference("editor") == 1
    assert store.get_preference("editor") is None
    assert store.delete_preference() == 1


def test_tasks_and_steps_are_stored_and_trimmed(store: MemoryStore, config: Config) -> None:
    config.set("memory.max_history", 3)
    for index in range(5):
        task_id = f"task_{index}"
        store.start_task(task_id, f"command {index}", "test.intent", state="running")
        store.finish_task(
            task_id,
            state="completed",
            summary=f"summary {index}",
            steps=[{
                "index": 1,
                "title": "Step",
                "tool": "fs.write",
                "args": {"path": "x.txt"},
                "risk": 2,
                "state": "succeeded",
                "message": "ok",
                "verified": True,
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:00:01Z",
            }],
        )

    recent = store.recent_tasks(10)
    assert len(recent) == 3
    assert all(task["steps"] for task in recent)
    assert store.recent_tasks(1)[0]["id"] == "task_4"


def test_interrupted_tasks_are_marked_on_restart(store: MemoryStore, config: Config) -> None:
    store.start_task("task_running", "do something", "test.intent", state="running")
    restarted = MemoryStore(config)
    interrupted = restarted.interrupted_tasks()
    assert [task["id"] for task in interrupted] == ["task_running"]
    assert interrupted[0]["state"] == "recovery_required"


def test_disable_memory_keeps_data_but_stops_new_writes(store: MemoryStore, config: Config) -> None:
    store.set_preference("kept", "yes")
    store.set_enabled(False)
    assert store.status()["enabled"] is False
    assert store.get_preference("kept") is None
    with pytest.raises(RuntimeError):
        store.set_preference("blocked", "no")

    store.set_enabled(True)
    assert store.get_preference("kept") == "yes"


def test_export_and_forget_all(store: MemoryStore, workspace: Path) -> None:
    store.set_preference("name", "local user")
    store.start_task("task_1", "hello", "test.intent")
    store.finish_task("task_1", state="completed", summary="done", steps=[])
    store.add_training_example("open notepad", "apps.open", approved=True)

    target = workspace / "sandbox" / "memory-export.json"
    store.export_all(target)
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["preferences"][0]["key"] == "name"
    assert payload["tasks"][0]["id"] == "task_1"
    assert "personal data" in payload["note"]

    removed = store.forget_all(include_training=True)
    assert removed["tasks"] == 1
    assert removed["preferences"] == 1
    assert removed["examples"] == 1
    assert store.recent_tasks() == []
    assert store.list_preferences() == []
    assert store.training_examples() == []


def test_retention_purges_old_tasks(store: MemoryStore, config: Config) -> None:
    store.start_task("old_task", "old", "test.intent")
    store.finish_task("old_task", state="completed", summary="old", steps=[])
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE tasks SET created_at=datetime('now','-400 days') WHERE id='old_task'")
        conn.commit()

    deleted = store.purge()
    assert deleted["tasks"] == 1
    assert store.recent_tasks() == []


def test_disabled_store_does_not_create_schema(tmp_path: Path) -> None:
    cfg = Config.from_dict({"memory": {"enabled": False}}, tmp_path)
    store = MemoryStore(cfg)
    assert store.status()["enabled"] is False
    assert not store.db_path.exists()
