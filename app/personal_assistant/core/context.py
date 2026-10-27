"""Dependency bundle passed to every tool handler."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..audit import AuditLogger
from ..config import Config
from .paths import PathPolicy
from .recovery import RecoveryJournal
from .safety import CancellationToken, SafetyController

try:
    from ..memory.store import MemoryStore
except ImportError:  # pragma: no cover - memory is a core module here
    MemoryStore = object  # type: ignore[assignment,misc]


@dataclass
class ToolContext:
    config: Config
    path_policy: PathPolicy
    audit: AuditLogger
    safety: SafetyController
    token: CancellationToken
    recovery: RecoveryJournal
    task_id: str
    memory: "MemoryStore | None" = None
    logger: logging.Logger = logging.getLogger("personal_assistant.tools")

    def check_cancel(self) -> None:
        self.safety.assert_ready(self.token)

    @property
    def cancelled(self) -> bool:
        return self.token.cancelled or self.safety.halted
