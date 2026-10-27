"""Append-only local audit log in JSONL format, with secret redaction."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from .config import Config
from .util import redact, utc_now


class AuditLogger:
    def __init__(self, config: Config) -> None:
        self.path = Path(config.get("permissions.audit_log"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_bytes = int(config.get("permissions.audit_max_megabytes", 10)) * 1024 * 1024
        self._lock = threading.RLock()

    def log(self, event: str, **fields: Any) -> dict[str, Any]:
        record = {
            "timestamp": utc_now(),
            "event": str(event)[:120],
            **redact(fields),
        }
        line = json.dumps(record, ensure_ascii=False, default=str, separators=(",", ":"))
        with self._lock:
            self._rotate_if_needed(len(line.encode("utf-8")) + 1)
            with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return record

    def _rotate_if_needed(self, incoming: int) -> None:
        try:
            if self.path.exists() and self.path.stat().st_size + incoming > self.max_bytes:
                rotated = self.path.with_suffix(self.path.suffix + ".1")
                if rotated.exists():
                    rotated.unlink()
                self.path.replace(rotated)
        except OSError:
            # Audit writes should not crash the assistant; the error is surfaced by diagnostics.
            pass

    def tail(self, count: int = 50) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        count = max(1, min(int(count), 1000))
        lines: list[str] = []
        try:
            with open(self.path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                end = handle.tell()
                block = 8192
                data = b""
                while end > 0 and data.count(b"\n") <= count:
                    step = min(block, end)
                    end -= step
                    handle.seek(end)
                    data = handle.read(step) + data
                lines = data.decode("utf-8", errors="replace").splitlines()[-count:]
        except OSError:
            return []
        records = []
        for line in lines:
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    records.append(parsed)
            except json.JSONDecodeError:
                continue
        return records
