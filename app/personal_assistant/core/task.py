"""Multi-step task state machine with verification and recovery reporting.

Task states: pending -> awaiting_approval -> running -> completed,
completed_unverified, failed, cancelled, or blocked. Every step is executed
through ``ToolExecutor``; tools perform their own post-condition checks and
report ``verified`` honestly. The engine never claims success it cannot support.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import Config
from ..errors import (
    AssistantError,
    PermissionDenied,
    SafetyHalted,
    TaskCancelled,
    ValidationError,
)
from ..util import new_id, utc_now
from .executor import ToolExecutor
from .permissions import ApprovalGrant, PermissionPolicy
from .planner import ExecutionPlan, PlanStep
from .recovery import RecoveryJournal
from .results import ToolResult
from .safety import CancellationToken, SafetyController

logger = logging.getLogger(__name__)


class TaskState:
    PENDING = "pending"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_UNVERIFIED = "completed_unverified"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"

    TERMINAL = {COMPLETED, COMPLETED_UNVERIFIED, FAILED, CANCELLED, BLOCKED}


_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    TaskState.PENDING: {
        TaskState.AWAITING_APPROVAL,
        TaskState.RUNNING,
        TaskState.BLOCKED,
        TaskState.CANCELLED,
        TaskState.FAILED,
    },
    TaskState.AWAITING_APPROVAL: {
        TaskState.RUNNING,
        TaskState.BLOCKED,
        TaskState.CANCELLED,
        TaskState.FAILED,
    },
    TaskState.RUNNING: {
        TaskState.COMPLETED,
        TaskState.COMPLETED_UNVERIFIED,
        TaskState.FAILED,
        TaskState.CANCELLED,
        TaskState.BLOCKED,
    },
    TaskState.COMPLETED: set(),
    TaskState.COMPLETED_UNVERIFIED: set(),
    TaskState.FAILED: set(),
    TaskState.CANCELLED: set(),
    TaskState.BLOCKED: set(),
}


class StepState:
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    UNVERIFIED = "unverified"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


@dataclass
class TaskStep:
    index: int
    title: str
    tool: str
    args: dict[str, Any]
    risk: int
    undoable: bool
    state: str = StepState.PENDING
    message: str = ""
    verified: bool | None = None
    started_at: str = ""
    finished_at: str = ""
    result: ToolResult | None = field(default=None, repr=False)

    def to_memory(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "title": self.title,
            "tool": self.tool,
            "args": self.args,
            "risk": self.risk,
            "state": self.state,
            "message": self.message,
            "verified": self.verified,
            "started_at": self.started_at or None,
            "finished_at": self.finished_at or None,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.to_memory(),
            "result": self.result.to_dict() if self.result else None,
        }


@dataclass
class TaskOutcome:
    task_id: str
    intent: str
    state: str
    summary: str
    message: str
    steps: list[TaskStep]
    started_at: str
    finished_at: str
    timed_out: bool = False
    undo_available: bool = False
    error: str = ""
    source: str = "rules"
    confidence: float = 1.0

    @property
    def ok(self) -> bool:
        return self.state in {TaskState.COMPLETED, TaskState.COMPLETED_UNVERIFIED}

    @property
    def verified(self) -> bool:
        return self.state == TaskState.COMPLETED and all(
            step.state == StepState.SUCCEEDED for step in self.steps
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "intent": self.intent,
            "state": self.state,
            "summary": self.summary,
            "message": self.message,
            "steps": [step.to_dict() for step in self.steps],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "timed_out": self.timed_out,
            "undo_available": self.undo_available,
            "error": self.error,
            "source": self.source,
            "confidence": self.confidence,
        }


class TaskEngine:
    def __init__(
        self,
        *,
        config: Config,
        executor: ToolExecutor,
        policy: PermissionPolicy,
        safety: SafetyController,
        recovery: RecoveryJournal,
        memory: Any = None,
    ) -> None:
        self.config = config
        self.executor = executor
        self.policy = policy
        self.safety = safety
        self.recovery = recovery
        self.memory = memory
        self.audit = executor.audit

    # State machine ---------------------------------------------------------
    @staticmethod
    def _transition(state: str, new_state: str) -> str:
        if new_state not in _ALLOWED_TRANSITIONS.get(state, set()):
            raise RuntimeError(f"Invalid task state transition: {state} -> {new_state}")
        return new_state

    def execute(
        self,
        plan: ExecutionPlan,
        *,
        command: str = "",
        token: CancellationToken | None = None,
    ) -> TaskOutcome:
        if not plan.steps:
            raise ValidationError("A task needs at least one step.")
        max_steps = int(self.config.get("app.max_steps", 40))
        if len(plan.steps) > max_steps:
            raise ValidationError(f"The task exceeds the configured limit of {max_steps} steps.")

        task_id = new_id("task_")
        started_at = utc_now()
        started_monotonic = time.monotonic()
        steps = [
            TaskStep(
                index=step.index,
                title=step.title,
                tool=step.tool,
                args=step.args,
                risk=step.risk,
                undoable=step.undoable,
            )
            for step in plan.steps
        ]
        state = TaskState.PENDING
        timeout_seconds = max(1, int(self.config.get("app.max_task_seconds", 600)))
        created_token = token is None
        token = token or self.safety.new_token(task_id)
        timed_out = False
        timer: threading.Timer | None = None
        error = ""
        approval_grant: ApprovalGrant | None = None
        approval_requested = any(
            self.policy.requires_approval(step.risk) or not self.policy.is_tool_allowed(step.tool)
            for step in plan.steps
        )

        if self.memory is not None:
            self.memory.start_task(task_id, command, plan.intent, state=TaskState.PENDING)
        self.audit.log(
            "task_started",
            task_id=task_id,
            intent=plan.intent,
            state=state,
            steps=len(steps),
            highest_risk=plan.highest_risk,
            source=plan.source,
        )

        try:
            if token.cancelled:
                state = self._transition(state, TaskState.CANCELLED)
                error = token.reason or "The task was cancelled before it started."
                raise TaskCancelled(error)

            if approval_requested:
                state = self._transition(state, TaskState.AWAITING_APPROVAL)
                if self.memory is not None:
                    self.memory.update_task_state(task_id, state)
                self.audit.log("task_awaiting_approval", task_id=task_id, state=state)
                try:
                    approval_grant = self.policy.preflight_plan(
                        [(step.spec, step.args) for step in plan.steps],
                        task_id,
                    )
                except PermissionDenied as exc:
                    state = self._transition(state, TaskState.BLOCKED)
                    error = str(exc)
                    raise
                except TaskCancelled:
                    state = self._transition(state, TaskState.CANCELLED)
                    error = "The task was cancelled while awaiting approval."
                    raise
            state = self._transition(state, TaskState.RUNNING)
            if self.memory is not None:
                self.memory.update_task_state(task_id, state)
            self.audit.log("task_running", task_id=task_id, state=state)

            def expire() -> None:
                nonlocal timed_out
                timed_out = True
                token.cancel(f"The task exceeded its {timeout_seconds} second time limit.")

            timer = threading.Timer(timeout_seconds, expire)
            timer.daemon = True
            timer.start()

            had_unverified = False
            failed_step: TaskStep | None = None
            cancelled = False
            for step in steps:
                if token.cancelled:
                    step.state = StepState.CANCELLED
                    step.message = token.reason or "Cancelled."
                    cancelled = True
                    break
                step.state = StepState.RUNNING
                step.started_at = utc_now()
                try:
                    result = self.executor.execute(
                        step.tool,
                        step.args,
                        token=token,
                        task_id=task_id,
                        grant=approval_grant,
                    )
                except TaskCancelled as exc:
                    step.state = StepState.CANCELLED
                    step.message = str(exc)
                    step.finished_at = utc_now()
                    cancelled = True
                    break
                except SafetyHalted as exc:
                    step.state = StepState.CANCELLED
                    step.message = str(exc)
                    step.finished_at = utc_now()
                    state = self._transition(state, TaskState.BLOCKED)
                    error = str(exc)
                    break
                except PermissionDenied as exc:
                    step.state = StepState.FAILED
                    step.message = str(exc)
                    step.finished_at = utc_now()
                    state = self._transition(state, TaskState.BLOCKED)
                    error = str(exc)
                    break
                except AssistantError as exc:
                    step.state = StepState.FAILED
                    step.message = str(exc)
                    step.finished_at = utc_now()
                    failed_step = step
                    error = str(exc)
                    break
                except Exception:
                    logger.exception("Unhandled task step failure")
                    step.state = StepState.FAILED
                    step.message = "An unexpected local error stopped this step."
                    step.finished_at = utc_now()
                    failed_step = step
                    error = step.message
                    break

                step.result = result
                step.verified = result.verified
                step.finished_at = utc_now()
                step.message = result.message
                if result.status == "cancelled":
                    step.state = StepState.CANCELLED
                    cancelled = True
                    break
                if result.status == "error" or result.status == "unavailable":
                    step.state = StepState.FAILED
                    failed_step = step
                    error = result.message
                    break
                if result.status == "ok" and result.verified is True:
                    step.state = StepState.SUCCEEDED
                else:
                    step.state = StepState.UNVERIFIED
                    had_unverified = True

            timer.cancel()
            if timed_out:
                timer.join(timeout=0.2)
            for step in steps:
                if step.state in {StepState.PENDING, StepState.RUNNING}:
                    step.state = StepState.SKIPPED
                    step.finished_at = step.finished_at or utc_now()

            if state == TaskState.BLOCKED:
                pass
            elif cancelled or timed_out:
                if state == TaskState.RUNNING:
                    state = self._transition(state, TaskState.CANCELLED)
                if timed_out:
                    error = error or f"The task exceeded its {timeout_seconds} second time limit."
            elif failed_step is not None:
                if state == TaskState.RUNNING:
                    state = self._transition(state, TaskState.FAILED)
            else:
                if state == TaskState.RUNNING:
                    state = self._transition(
                        state,
                        TaskState.COMPLETED_UNVERIFIED if had_unverified else TaskState.COMPLETED,
                    )

            # A terminal state reached by an exception path may still be running.
            if state not in TaskState.TERMINAL:
                state = self._transition(state, TaskState.FAILED)
                error = error or "The task ended without reaching a terminal state."
        except TaskCancelled as exc:
            error = error or str(exc)
            if state not in TaskState.TERMINAL:
                state = self._transition(state, TaskState.CANCELLED)
        except SafetyHalted as exc:
            error = error or str(exc)
            if state not in TaskState.TERMINAL:
                state = self._transition(state, TaskState.BLOCKED)
        except PermissionDenied as exc:
            error = error or str(exc)
            if state not in TaskState.TERMINAL:
                state = self._transition(state, TaskState.BLOCKED)
        except AssistantError as exc:
            error = error or str(exc)
            if state not in TaskState.TERMINAL:
                state = self._transition(state, TaskState.FAILED)
        except Exception:
            logger.exception("Unhandled task failure")
            error = error or "An unexpected local error stopped the task."
            if state not in TaskState.TERMINAL:
                state = self._transition(state, TaskState.FAILED)
        finally:
            if timer is not None:
                timer.cancel()
            if created_token:
                self.safety.release_token(token)
            for step in steps:
                if step.state in {StepState.PENDING, StepState.RUNNING}:
                    step.state = StepState.SKIPPED
                    step.finished_at = step.finished_at or utc_now()

        finished_at = utc_now()
        undo_available = self._has_undo_for(task_id)
        summary = self._summary(state, steps, error, timed_out, undo_available)
        message = self._details(state, steps, error, timed_out, undo_available)
        outcome = TaskOutcome(
            task_id=task_id,
            intent=plan.intent,
            state=state,
            summary=summary,
            message=message,
            steps=steps,
            started_at=started_at,
            finished_at=finished_at,
            timed_out=timed_out,
            undo_available=undo_available,
            error=error,
            source=plan.source,
            confidence=plan.confidence,
        )
        self.audit.log(
            "task_finished",
            task_id=task_id,
            state=state,
            verified=outcome.verified,
            duration_ms=int((time.monotonic() - started_monotonic) * 1000),
            undo_available=undo_available,
            error=error[:500],
        )
        if self.memory is not None:
            self.memory.finish_task(
                task_id,
                state=state,
                summary=summary,
                steps=[step.to_memory() for step in steps],
                command=command,
                intent=plan.intent,
                created_at=started_at,
            )
        return outcome

    def _has_undo_for(self, task_id: str) -> bool:
        try:
            entries = self.recovery.list_entries(500)
        except Exception:
            return False
        return any(
            entry.get("task_id") == task_id
            and entry.get("undoable")
            and not entry.get("undone")
            for entry in entries
        )

    @staticmethod
    def _summary(
        state: str,
        steps: list[TaskStep],
        error: str,
        timed_out: bool,
        undo_available: bool,
    ) -> str:
        completed = sum(1 for step in steps if step.state == StepState.SUCCEEDED)
        unverified = sum(1 for step in steps if step.state == StepState.UNVERIFIED)
        if state == TaskState.COMPLETED:
            text = f"Completed {len([s for s in steps if s.state in {StepState.SUCCEEDED, StepState.UNVERIFIED}])} step(s) and verified the result."
        elif state == TaskState.COMPLETED_UNVERIFIED:
            text = (
                f"Completed the task with {unverified} unverified step(s). "
                "The actions ran, but the assistant cannot confirm every result."
            )
        elif state == TaskState.CANCELLED:
            text = "The task was cancelled."
            if timed_out:
                text = "The task timed out and was cancelled."
        elif state == TaskState.BLOCKED:
            text = (
                "The task was blocked before completion. Unapproved actions were not run; "
                "review the step details for any work that completed first."
            )
        elif state == TaskState.FAILED:
            text = f"The task failed after {completed} verified step(s)."
        else:
            text = "The task did not complete."
        if state == TaskState.FAILED:
            text += " " + (error or "A step reported an error.")
        if undo_available:
            text += " Undo is available for the supported changes."
        return text.strip()

    @staticmethod
    def _details(
        state: str,
        steps: list[TaskStep],
        error: str,
        timed_out: bool,
        undo_available: bool,
    ) -> str:
        lines: list[str] = []
        for step in steps:
            marker = {
                StepState.SUCCEEDED: "OK",
                StepState.UNVERIFIED: "UNVERIFIED",
                StepState.FAILED: "FAILED",
                StepState.CANCELLED: "CANCELLED",
                StepState.SKIPPED: "SKIPPED",
                StepState.PENDING: "NOT RUN",
                StepState.RUNNING: "STOPPED",
            }.get(step.state, step.state.upper())
            line = f"{step.index}. {step.title}: {marker}"
            if step.message:
                line += f" — {step.message}"
            lines.append(line)
        if error and state in {TaskState.FAILED, TaskState.BLOCKED, TaskState.CANCELLED}:
            lines.append(f"Reason: {error}")
        if undo_available:
            lines.append("Use 'undo' to reverse the most recent supported filesystem change.")
        return "\n".join(lines)



