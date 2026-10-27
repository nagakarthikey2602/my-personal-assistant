"""Central authorization policy and approval flow."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import Config
from ..errors import PermissionDenied, ValidationError
from .registry import RiskLevel, ToolSpec

logger = logging.getLogger(__name__)


@dataclass
class ApprovalRequest:
    task_id: str
    tool: str
    title: str
    risk: int
    description: str
    reason: str
    args_preview: str
    undoable: bool = False

    @property
    def risk_label(self) -> str:
        return RiskLevel.label(self.risk)


@dataclass
class ApprovalDecision:
    granted: bool
    reason: str = ""
    granted_level: int = 0


@dataclass
class ApprovalGrant:
    task_id: str
    level: int
    request: ApprovalRequest | None = None


ApprovalCallback = Callable[[ApprovalRequest], bool]


class PermissionPolicy:
    def __init__(
        self,
        config: Config,
        approval_callback: ApprovalCallback | None = None,
        *,
        deny_by_default: bool = True,
    ) -> None:
        self.config = config
        self.approval_callback = approval_callback
        self.deny_by_default = deny_by_default
        self.auto_approve_max = int(config.get("app.auto_approve_max_risk", 2))
        self.allowed_tools = list(config.get("permissions.allowed_tools", ["*"]))
        self.denied_tools = set(config.get("permissions.denied_tools", []))

    def is_tool_allowed(self, name: str) -> bool:
        if name in self.denied_tools:
            return False
        return "*" in self.allowed_tools or name in self.allowed_tools

    def requires_approval(self, risk: int) -> bool:
        confirm_3 = bool(self.config.get("app.confirm_level_3", True))
        confirm_4 = bool(self.config.get("app.confirm_level_4", True))
        if risk >= 4:
            return risk > self.auto_approve_max or confirm_4
        if risk == 3:
            return risk > self.auto_approve_max or confirm_3
        return risk > self.auto_approve_max

    def approval_request(
        self,
        tool: ToolSpec,
        args: dict[str, Any],
        task_id: str,
        *,
        reason: str = "",
    ) -> ApprovalRequest:
        preview = ", ".join(f"{key}={str(value)[:120]}" for key, value in args.items())
        return ApprovalRequest(
            task_id=task_id,
            tool=tool.name,
            title=tool.description,
            risk=tool.effective_risk(args),
            description=tool.description,
            reason=reason or "This action is above the automatic approval level.",
            args_preview=preview[:600],
            undoable=tool.undoable,
        )

    def authorize(
        self,
        tool: ToolSpec,
        args: dict[str, Any],
        *,
        task_id: str,
        grant: ApprovalGrant | None = None,
    ) -> ApprovalDecision:
        if not self.is_tool_allowed(tool.name):
            raise PermissionDenied(f"The tool '{tool.name}' is not on the tool allowlist.")
        risk = tool.effective_risk(args)
        if grant is not None and grant.task_id == task_id and grant.level >= risk:
            return ApprovalDecision(True, "Approved for this task.", risk)
        if risk <= self.auto_approve_max and not self.requires_approval(risk):
            return ApprovalDecision(True, f"Automatically allowed ({RiskLevel.label(risk)}).", risk)
        if self.approval_callback is None:
            if self.deny_by_default:
                raise PermissionDenied(
                    f"'{tool.name}' is {RiskLevel.label(risk)} and needs explicit approval, "
                    "but no approval interface is available."
                )
            return ApprovalDecision(True, "No callback configured; deny_by_default is disabled.", risk)
        request = self.approval_request(tool, args, task_id)
        try:
            granted = bool(self.approval_callback(request))
        except Exception:
            logger.exception("Approval callback failed")
            granted = False
        if not granted:
            raise PermissionDenied(f"Approval was not granted for {tool.name}.")
        return ApprovalDecision(True, "Approved by the user.", risk)

    def preflight_plan(
        self,
        steps: list[tuple[ToolSpec, dict[str, Any]]],
        task_id: str,
    ) -> ApprovalGrant | None:
        """Ask once for the highest-risk action in a planned task."""
        if not steps:
            return None
        highest = 0
        chosen: tuple[ToolSpec, dict[str, Any]] | None = None
        for spec, args in steps:
            if not self.is_tool_allowed(spec.name):
                raise PermissionDenied(f"The tool '{spec.name}' is not on the tool allowlist.")
            risk = spec.effective_risk(args)
            if risk > highest:
                highest = risk
                chosen = (spec, args)
        if highest == 0 or (highest <= self.auto_approve_max and not self.requires_approval(highest)):
            return None
        if self.approval_callback is None:
            if self.deny_by_default:
                raise PermissionDenied(
                    f"This task includes a {RiskLevel.label(highest)} action and needs approval, "
                    "but no approval interface is available."
                )
            return ApprovalGrant(task_id=task_id, level=highest)
        assert chosen is not None
        request = self.approval_request(
            chosen[0],
            chosen[1],
            task_id,
            reason=f"Task contains a {RiskLevel.label(highest)} action.",
        )
        if not self.approval_callback(request):
            raise PermissionDenied("The task was not approved.")
        return ApprovalGrant(task_id=task_id, level=highest, request=request)


class AutoApprover:
    """Test helper / CLI auto-approval for explicitly unattended operation."""

    def __init__(self, grant: bool = True) -> None:
        self.grant = grant
        self.requests: list[ApprovalRequest] = []

    def __call__(self, request: ApprovalRequest) -> bool:
        self.requests.append(request)
        return self.grant


class ApprovalBridge:
    """Thread-safe callback bridge between a worker thread and the UI.

    The UI sets a callback that schedules a dialog on the main thread and calls
    ``resolve`` with the user's answer. A worker can then block on ``ask``.
    """

    def __init__(self, timeout_seconds: float = 120.0) -> None:
        import threading

        self._lock = threading.Lock()
        self._event = threading.Event()
        self._answer = False
        self._pending: ApprovalRequest | None = None
        self._notify: Callable[[ApprovalRequest], None] | None = None
        self.timeout = timeout_seconds

    def set_notifier(self, notifier: Callable[[ApprovalRequest], None]) -> None:
        self._notify = notifier

    def ask(self, request: ApprovalRequest) -> bool:
        with self._lock:
            self._pending = request
            self._event.clear()
            self._answer = False
        if self._notify is None:
            return False
        self._notify(request)
        if not self._event.wait(timeout=self.timeout):
            with self._lock:
                self._pending = None
            return False
        with self._lock:
            self._pending = None
            return self._answer

    def resolve(self, granted: bool) -> None:
        with self._lock:
            self._answer = bool(granted)
            self._event.set()

    def is_pending(self) -> bool:
        with self._lock:
            return self._pending is not None
