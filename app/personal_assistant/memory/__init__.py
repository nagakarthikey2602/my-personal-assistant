"""Local SQLite memory for preferences, task history, and approved corrections."""

from .store import MemoryStore

__all__ = ["MemoryStore"]
