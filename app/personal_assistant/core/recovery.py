"""Persistent undo journal for supported filesystem operations.

The journal is independent of optional memory. Every recorded action stores enough
information to reverse one operation. Undo is best-effort and explicitly does not
cover application actions, browser actions, typing, or external systems.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path
from typing import Any

from ..config import Config
from ..errors import VerificationError
from ..util import atomic_write_text, new_id, sha256_file, utc_now
from .paths import PathPolicy

MAX_ENTRIES = 500


class RecoveryJournal:
    def __init__(self, config: Config, path_policy: PathPolicy) -> None:
        self.config = config
        self.path_policy = path_policy
        self.path = Path(config.get("paths.recovery_journal"))
        self.quarantine_root = Path(config.get("paths.quarantine"))
        self.backups_root = Path(config.get("paths.backups"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.quarantine_root.mkdir(parents=True, exist_ok=True)
        self.backups_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {"version": 1, "entries": []}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("entries"), list):
                self._data = loaded
        except (OSError, json.JSONDecodeError):
            self._data = {"version": 1, "entries": []}

    def _save(self) -> None:
        entries = self._data["entries"][-MAX_ENTRIES:]
        self._data["entries"] = entries
        atomic_write_text(self.path, json.dumps(self._data, ensure_ascii=False, indent=2))

    def record(
        self,
        *,
        description: str,
        tool: str,
        task_id: str,
        actions: list[dict[str, Any]],
        undoable: bool = True,
    ) -> str:
        with self._lock:
            entry = {
                "id": new_id("undo_"),
                "timestamp": utc_now(),
                "task_id": task_id,
                "tool": tool,
                "description": description,
                "actions": actions if undoable else [],
                "undoable": bool(undoable and actions),
                "undone": False,
                "undone_at": None,
            }
            self._data["entries"].append(entry)
            self._save()
            return str(entry["id"])

    def list_entries(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return list(reversed(self._data["entries"][-max(1, min(limit, MAX_ENTRIES)):]))

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return [entry for entry in self._data["entries"] if not entry.get("undone")]

    def _find_last_undoable(self) -> tuple[int, dict[str, Any]]:
        with self._lock:
            for index in range(len(self._data["entries"]) - 1, -1, -1):
                entry = self._data["entries"][index]
                if entry.get("undone"):
                    continue
                if entry.get("undoable") and entry.get("actions"):
                    return index, entry
                raise VerificationError(
                    f"The most recent operation ({entry.get('description', 'unknown')}) "
                    "is not undoable. No older operation was reversed."
                )
        raise VerificationError("There is no supported operation to undo.")

    def undo_last(self) -> dict[str, Any]:
        index, entry = self._find_last_undoable()
        actions = list(reversed(entry.get("actions", [])))
        completed = 0
        try:
            for action in actions:
                self._apply_undo_action(action)
                completed += 1
        except Exception as exc:
            raise VerificationError(
                f"Undo stopped after {completed} action(s); the remaining state has been kept. "
                f"Reason: {exc}"
            ) from exc
        with self._lock:
            self._data["entries"][index]["undone"] = True
            self._data["entries"][index]["undone_at"] = utc_now()
            self._save()
        return {
            "description": entry.get("description"),
            "actions": completed,
            "timestamp": entry.get("timestamp"),
        }

    def _require_path(self, raw: str, *, write: bool) -> Path:
        policy = self.path_policy
        return policy.check_write(raw) if write else policy.check_read(raw)

    def _apply_undo_action(self, action: dict[str, Any]) -> None:
        kind = action.get("op")
        if kind == "move":
            source = Path(str(action["source"]))
            destination = Path(str(action["destination"]))
            # Source is often inside internal quarantine; validate destination via policy.
            self._require_path(str(destination), write=True)
            if not source.exists():
                raise VerificationError(f"Recovery source is missing: {source.name}")
            if destination.exists():
                raise VerificationError(f"Refusing to overwrite during undo: {destination.name}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            if not destination.exists() or source.exists():
                raise VerificationError("Undo move could not be verified.")
            return
        if kind == "remove_file":
            target = Path(str(action["path"]))
            self._require_path(str(target), write=True)
            if not target.exists() or not target.is_file():
                raise VerificationError(f"File to remove is missing: {target.name}")
            expected = action.get("sha256")
            if expected and sha256_file(target) != expected:
                raise VerificationError("The file changed after the original operation; undo refused.")
            trash = self.quarantine_root / new_id("undo_removed_")
            trash.mkdir(parents=True, exist_ok=True)
            destination = trash / target.name
            shutil.move(str(target), str(destination))
            return
        if kind == "remove_dir":
            target = Path(str(action["path"]))
            self._require_path(str(target), write=True)
            if target.exists():
                if not target.is_dir():
                    raise VerificationError(f"Expected a directory: {target.name}")
                try:
                    target.rmdir()  # only succeeds if empty
                except OSError as exc:
                    raise VerificationError(
                        f"Directory {target.name} is not empty; undo refused to delete contents."
                    ) from exc
            return
        if kind == "restore_backup":
            backup = Path(str(action["backup_path"]))
            original = Path(str(action["original_path"]))
            self._require_path(str(original), write=True)
            if not backup.exists():
                raise VerificationError("The backup for this undo is missing.")
            if original.exists():
                raise VerificationError(f"Refusing to overwrite existing file during restore: {original.name}")
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(backup), str(original))
            return
        if kind == "quarantine_path":
            target = Path(str(action["path"]))
            self._require_path(str(target), write=True)
            if not target.exists():
                raise VerificationError(f"Path to undo is missing: {target.name}")
            expected = action.get("sha256")
            if expected and target.is_file() and sha256_file(target) != expected:
                raise VerificationError("The file changed after the original operation; undo refused.")
            trash = self.quarantine_root / new_id("undo_")
            trash.mkdir(parents=True, exist_ok=True)
            destination = trash / target.name
            shutil.move(str(target), str(destination))
            if target.exists() or not destination.exists():
                raise VerificationError("Undo could not move the item into quarantine.")
            return
        raise VerificationError(f"Unsupported undo action: {kind!r}")

    def status(self) -> dict[str, Any]:
        entries = self.list_entries(1000)
        return {
            "path": str(self.path),
            "entries": len(entries),
            "pending": sum(1 for entry in entries if not entry.get("undone")),
            "last": entries[0] if entries else None,
            "quarantine_root": str(self.quarantine_root),
        }


def directory_fingerprint(root: Path) -> dict[str, Any]:
    """A cheap recursive fingerprint used to verify copy/move operations."""
    files = 0
    total = 0
    digest_samples: list[str] = []
    for current, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(names):
            path = Path(current) / name
            try:
                stat = path.stat()
            except OSError:
                continue
            files += 1
            total += stat.st_size
            if len(digest_samples) < 5:
                digest_samples.append(f"{path.relative_to(root)}:{stat.st_size}")
    return {"files": files, "bytes": total, "samples": digest_samples}
