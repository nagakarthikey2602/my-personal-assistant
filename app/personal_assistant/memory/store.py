"""SQLite persistence with explicit, inspectable tables and retention controls."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from ..config import Config
from ..util import atomic_write_text, new_id, utc_now

logger = logging.getLogger(__name__)
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class MemoryStore:
    def __init__(self, config: Config, *, initialize: bool = True) -> None:
        self.config = config
        self.db_path = Path(config.get("paths.database"))
        self.enabled = bool(config.get("memory.enabled", True))
        self.store_commands = bool(config.get("memory.store_commands", True))
        self.store_outputs = bool(config.get("memory.store_outputs", False))
        self._lock = threading.RLock()
        if initialize:
            self.initialize()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def initialize(self) -> None:
        if not self.enabled:
            return
        with self._lock, self._connect() as conn:
            schema = _SCHEMA_PATH.read_text(encoding="utf-8")
            conn.executescript(schema)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version','1') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
        self._mark_interrupted()
        self.purge()

    def _require_enabled(self) -> bool:
        return bool(self.enabled and self.config.get("memory.enabled", True))

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        self.config.set("memory.enabled", self.enabled)
        if self.enabled:
            self.initialize()

    def _mark_interrupted(self) -> None:
        if not self._require_enabled():
            return
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    "UPDATE tasks SET state='recovery_required', interrupted=1, "
                    "finished_at=COALESCE(finished_at, ?) "
                    "WHERE state IN ('running','awaiting_approval','pending')",
                    (utc_now(),),
                )
        except sqlite3.Error:
            logger.exception("Could not mark interrupted tasks")

    def start_task(self, task_id: str, command: str, intent: str, state: str = "running") -> None:
        if not self._require_enabled():
            return
        stored_command = command if self.store_commands else None
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO tasks(id, command, intent, state, created_at) "
                    "VALUES(?,?,?,?,?)",
                    (task_id, stored_command, intent, state, utc_now()),
                )
        except sqlite3.Error:
            logger.exception("Could not record task start")

    def finish_task(
        self,
        task_id: str,
        *,
        state: str,
        summary: str,
        steps: Iterable[dict[str, Any]],
        command: str = "",
        intent: str = "",
        created_at: str | None = None,
    ) -> None:
        if not self._require_enabled():
            return
        stored_command = command if self.store_commands else None
        summary_stored = summary if self.store_outputs else summary[:1000]
        try:
            with self._lock, self._connect() as conn:
                existing = conn.execute("SELECT id FROM tasks WHERE id=?", (task_id,)).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE tasks SET state=?, summary=?, finished_at=?, command=COALESCE(?, command), "
                        "intent=COALESCE(NULLIF(?,''), intent) WHERE id=?",
                        (state, summary_stored, utc_now(), stored_command, intent, task_id),
                    )
                    conn.execute("DELETE FROM steps WHERE task_id=?", (task_id,))
                else:
                    conn.execute(
                        "INSERT INTO tasks(id, command, intent, state, summary, created_at, finished_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (task_id, stored_command, intent, state, summary_stored, created_at or utc_now(), utc_now()),
                    )
                for step in steps:
                    args_json = json.dumps(step.get("args", {}), ensure_ascii=False, default=str)
                    if len(args_json) > 2000:
                        args_json = args_json[:2000] + "...[truncated]"
                    conn.execute(
                        "INSERT INTO steps(task_id, step_index, title, tool, args_json, risk, state, "
                        "message, verified, started_at, finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            task_id,
                            int(step.get("index", 0)),
                            str(step.get("title", ""))[:300],
                            str(step.get("tool", ""))[:120],
                            args_json,
                            int(step.get("risk", 1)),
                            str(step.get("state", ""))[:40],
                            str(step.get("message", ""))[:1500],
                            1 if step.get("verified") else (0 if step.get("verified") is False else None),
                            step.get("started_at"),
                            step.get("finished_at"),
                        ),
                    )
            self._trim_history()
        except sqlite3.Error:
            logger.exception("Could not record task result")

    def update_task_state(self, task_id: str, state: str, summary: str = "") -> None:
        if not self._require_enabled():
            return
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    "UPDATE tasks SET state=?, summary=COALESCE(NULLIF(?,''), summary), "
                    "finished_at=CASE WHEN ? IN ('completed','failed','cancelled') THEN ? ELSE finished_at END "
                    "WHERE id=?",
                    (state, summary, state, utc_now(), task_id),
                )
        except sqlite3.Error:
            logger.exception("Could not update task state")

    def recent_tasks(self, limit: int = 20, include_steps: bool = True) -> list[dict[str, Any]]:
        if not self._require_enabled():
            return []
        limit = max(1, min(int(limit), 200))
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM tasks ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
                ).fetchall()
                tasks = [dict(row) for row in rows]
                if include_steps:
                    for task in tasks:
                        step_rows = conn.execute(
                            "SELECT * FROM steps WHERE task_id=? ORDER BY step_index", (task["id"],)
                        ).fetchall()
                        task["steps"] = [dict(step) for step in step_rows]
                return tasks
        except sqlite3.Error:
            logger.exception("Could not read task history")
            return []

    def interrupted_tasks(self) -> list[dict[str, Any]]:
        return [task for task in self.recent_tasks(100) if task.get("interrupted")]

    def _trim_history(self) -> None:
        max_history = int(self.config.get("memory.max_history", 1000))
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM tasks WHERE id IN ("
                "SELECT id FROM tasks ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?)",
                (max_history,),
            )

    def purge(self) -> dict[str, int]:
        if not self._require_enabled():
            return {"tasks": 0, "examples": 0}
        retention = int(self.config.get("memory.retention_days", 90))
        deleted = {"tasks": 0, "examples": 0}
        try:
            with self._lock, self._connect() as conn:
                if retention > 0:
                    cutoff = f"-{retention} days"
                    cur = conn.execute(
                        "DELETE FROM tasks WHERE created_at < datetime('now', ?)", (cutoff,)
                    )
                    deleted["tasks"] = cur.rowcount
                    cur = conn.execute(
                        "DELETE FROM training_examples WHERE created_at < datetime('now', ?)", (cutoff,)
                    )
                    deleted["examples"] = cur.rowcount
                    conn.execute("DELETE FROM steps WHERE task_id NOT IN (SELECT id FROM tasks)")
        except sqlite3.Error:
            logger.exception("Could not purge memory")
        return deleted

    # Preferences ---------------------------------------------------------
    def set_preference(self, key: str, value: Any, source: str = "user") -> None:
        if not self._require_enabled():
            raise RuntimeError("Memory is disabled.")
        now = utc_now()
        encoded = json.dumps(value, ensure_ascii=False, default=str)
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO preferences(key, value, source, created_at, updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, source=excluded.source, updated_at=excluded.updated_at",
                (key.strip()[:200], encoded, source[:80], now, now),
            )

    def get_preference(self, key: str) -> Any | None:
        if not self._require_enabled():
            return None
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM preferences WHERE key=?", (key.strip()[:200],)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def list_preferences(self) -> list[dict[str, Any]]:
        if not self._require_enabled():
            return []
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM preferences ORDER BY key").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["value"] = json.loads(item["value"])
            except json.JSONDecodeError:
                pass
            result.append(item)
        return result

    def delete_preference(self, key: str | None = None) -> int:
        if not self._require_enabled():
            return 0
        with self._lock, self._connect() as conn:
            if key is None:
                cur = conn.execute("DELETE FROM preferences")
            else:
                cur = conn.execute("DELETE FROM preferences WHERE key=?", (key.strip()[:200],))
            return cur.rowcount

    # Training examples and inspection ------------------------------------
    def add_training_example(self, text: str, label: str, source: str = "user_correction", approved: bool = False) -> str:
        if not self._require_enabled():
            raise RuntimeError("Memory is disabled.")
        example_id = new_id("ex_")
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO training_examples(id, text, label, source, approved, created_at) VALUES(?,?,?,?,?,?)",
                (example_id, text[:2000], label[:100], source[:80], 1 if approved else 0, utc_now()),
            )
        return example_id

    def training_examples(self) -> list[dict[str, Any]]:
        if not self._require_enabled():
            return []
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM training_examples ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def export_all(self, destination: str | Path) -> Path:
        target = Path(destination)
        payload = {
            "exported_at": utc_now(),
            "version": 1,
            "preferences": self.list_preferences(),
            "tasks": self.recent_tasks(200, include_steps=True),
            "training_examples": self.training_examples(),
            "note": "This export contains local personal data. Store it privately.",
        }
        atomic_write_text(target, json.dumps(payload, ensure_ascii=False, indent=2))
        return target

    def forget_all(self, *, include_training: bool = False) -> dict[str, int]:
        if not self._require_enabled():
            return {"tasks": 0, "preferences": 0, "examples": 0}
        with self._lock, self._connect() as conn:
            before = {
                "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                "preferences": conn.execute("SELECT COUNT(*) FROM preferences").fetchone()[0],
                "examples": conn.execute("SELECT COUNT(*) FROM training_examples").fetchone()[0],
            }
            conn.execute("DELETE FROM steps")
            conn.execute("DELETE FROM tasks")
            conn.execute("DELETE FROM preferences")
            if include_training:
                conn.execute("DELETE FROM training_examples")
            conn.commit()
            conn.execute("VACUUM")
        return before

    def status(self) -> dict[str, Any]:
        enabled = self._require_enabled()
        status: dict[str, Any] = {
            "enabled": enabled,
            "database": str(self.db_path),
            "store_commands": self.store_commands,
            "retention_days": int(self.config.get("memory.retention_days", 90)),
        }
        if not enabled:
            return status
        try:
            with self._connect() as conn:
                counts = {
                    "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                    "preferences": conn.execute("SELECT COUNT(*) FROM preferences").fetchone()[0],
                    "training_examples": conn.execute("SELECT COUNT(*) FROM training_examples").fetchone()[0],
                }
            status.update(counts)
            status["size_bytes"] = self.db_path.stat().st_size if self.db_path.exists() else 0
        except sqlite3.Error as exc:
            status["error"] = f"database unavailable: {exc.__class__.__name__}"
        return status
