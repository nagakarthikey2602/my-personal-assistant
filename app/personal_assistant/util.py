"""Small shared helpers: timestamps, hashing, safe text, atomic writes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

_MAX_TEXT = 400
_SECRET_PATTERNS = [
    re.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key)\s*[:=]\s*(\S+)"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
]


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_id(prefix: str = "") -> str:
    value = uuid.uuid4().hex[:12]
    return f"{prefix}{value}"


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def redact(value: Any, max_len: int = _MAX_TEXT) -> Any:
    """Recursively redact obvious secrets and bound the size of logged values."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in ("password", "secret", "token", "api_key", "apikey")):
                result[str(key)] = "***REDACTED***"
            else:
                result[str(key)] = redact(item, max_len=max_len)
        return result
    if isinstance(value, (list, tuple)):
        return [redact(item, max_len=max_len) for item in value]
    if isinstance(value, str):
        text = value
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub(lambda m: m.group(0).split(m.group(2))[0] + "***REDACTED***" if m.lastindex else "***REDACTED***", text)
        if len(text) > max_len:
            return text[:max_len] + f"... [truncated {len(text) - max_len} chars]"
        return text
    if isinstance(value, Path):
        return str(value)
    return value


def safe_preview(text: str, limit: int = 800) -> str:
    cleaned = text.replace("\x00", "")
    if len(cleaned) > limit:
        return cleaned[:limit] + f"\n... [truncated, {len(cleaned) - limit} more characters]"
    return cleaned


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def format_bytes(value: int | float) -> str:
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(amount) < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{amount:.1f} TB"


def human_join(items: Iterable[str], limit: int = 5) -> str:
    values = list(items)
    if not values:
        return "none"
    if len(values) > limit:
        return ", ".join(values[:limit]) + f", and {len(values) - limit} more"
    if len(values) == 1:
        return values[0]
    return ", ".join(values[:-1]) + " and " + values[-1]


def ensure_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
