"""Structured tool/task results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..util import utc_now


@dataclass
class ToolResult:
    tool: str
    status: str  # ok | error | cancelled | unavailable | unverified
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    verified: bool | None = None
    started_at: str = field(default_factory=utc_now)
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status in {"ok", "unverified"}

    @property
    def succeeded_and_verified(self) -> bool:
        return self.status == "ok" and self.verified is True

    @classmethod
    def success(
        cls,
        tool: str,
        message: str,
        *,
        data: dict[str, Any] | None = None,
        verified: bool | None = True,
    ) -> "ToolResult":
        return cls(tool=tool, status="ok", message=message, data=data or {}, verified=verified)

    @classmethod
    def error(cls, tool: str, message: str, *, data: dict[str, Any] | None = None) -> "ToolResult":
        return cls(tool=tool, status="error", message=message, data=data or {}, verified=False)

    @classmethod
    def cancelled(cls, tool: str, message: str = "Cancelled.") -> "ToolResult":
        return cls(tool=tool, status="cancelled", message=message, verified=False)

    @classmethod
    def unverified(cls, tool: str, message: str, *, data: dict[str, Any] | None = None) -> "ToolResult":
        return cls(tool=tool, status="unverified", message=message, data=data or {}, verified=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "status": self.status,
            "message": self.message,
            "data": self.data,
            "verified": self.verified,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
        }
