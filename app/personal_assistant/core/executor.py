"""The single execution gate: validate, authorize, audit, run, and report."""

from __future__ import annotations

import logging
import time
from typing import Any

from ..audit import AuditLogger
from ..config import Config
from ..errors import (
    AssistantError,
    PermissionDenied,
    SafetyHalted,
    TaskCancelled,
    ToolUnavailable,
)
from ..util import redact, utc_now
from .context import ToolContext
from .paths import PathPolicy
from .permissions import ApprovalGrant, PermissionPolicy
from .recovery import RecoveryJournal
from .registry import RiskLevel, ToolRegistry, ToolSpec
from .results import ToolResult
from .safety import CancellationToken, SafetyController

logger = logging.getLogger(__name__)


class ToolExecutor:
    def __init__(
        self,
        *,
        config: Config,
        registry: ToolRegistry,
        policy: PermissionPolicy,
        path_policy: PathPolicy,
        audit: AuditLogger,
        safety: SafetyController,
        recovery: RecoveryJournal,
        memory: Any = None,
    ) -> None:
        self.config = config
        self.registry = registry
        self.policy = policy
        self.path_policy = path_policy
        self.audit = audit
        self.safety = safety
        self.recovery = recovery
        self.memory = memory

    def context(self, *, token: CancellationToken, task_id: str) -> ToolContext:
        return ToolContext(
            config=self.config,
            path_policy=self.path_policy,
            audit=self.audit,
            safety=self.safety,
            token=token,
            recovery=self.recovery,
            task_id=task_id,
            memory=self.memory,
        )

    def execute(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        token: CancellationToken,
        task_id: str = "adhoc",
        grant: ApprovalGrant | None = None,
    ) -> ToolResult:
        self.safety.assert_ready(token)
        if not isinstance(args, dict):
            raise AssistantError("Tool arguments must be a mapping.")
        spec = self.registry.get(tool_name)
        self.registry.validate_args(tool_name, args)
        risk = spec.effective_risk(args)
        if spec.enabled_setting and not self.config.get(spec.enabled_setting, True):
            raise ToolUnavailable(f"{spec.description} is disabled in configuration.")
        if spec.requires_network and self.config.get("app.offline_mode") == "strict":
            self.audit.log(
                "tool_denied",
                tool=tool_name,
                task_id=task_id,
                risk=risk,
                reason="network_required_in_strict_offline_mode",
            )
            raise PermissionDenied(
                f"{tool_name} requires network access, which is blocked by strict offline mode. "
                "It was not run."
            )
        started = time.monotonic()
        self.audit.log(
            "tool_requested",
            tool=tool_name,
            task_id=task_id,
            risk=risk,
            risk_label=RiskLevel.label(risk),
            args=redact(args),
        )
        try:
            decision = self.policy.authorize(spec, args, task_id=task_id, grant=grant)
        except (PermissionDenied, SafetyHalted, TaskCancelled) as exc:
            self.audit.log(
                "tool_denied",
                tool=tool_name,
                task_id=task_id,
                risk=risk,
                reason=str(exc)[:500],
            )
            raise
        context = self.context(token=token, task_id=task_id)
        self.safety.assert_ready(token)
        try:
            result = spec.handler(context, args)
            if not isinstance(result, ToolResult):
                raise TypeError(f"Tool {tool_name} returned an invalid result object")
        except (TaskCancelled, SafetyHalted) as exc:
            self.audit.log(
                "tool_cancelled",
                tool=tool_name,
                task_id=task_id,
                duration_ms=int((time.monotonic() - started) * 1000),
                reason=str(exc)[:300],
            )
            raise
        except AssistantError as exc:
            result = ToolResult.error(tool_name, str(exc))
        except Exception:
            logger.exception("Unhandled tool failure in %s", tool_name)
            result = ToolResult.error(
                tool_name,
                "An unexpected local error occurred. Details are in the local log.",
            )
        result.duration_ms = int((time.monotonic() - started) * 1000)
        self.audit.log(
            "tool_finished",
            tool=result.tool,
            task_id=task_id,
            status=result.status,
            verified=result.verified,
            duration_ms=result.duration_ms,
            message=result.message[:500],
            data_keys=sorted(result.data.keys()),
        )
        return result
