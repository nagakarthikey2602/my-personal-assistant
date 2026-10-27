"""Task state machine, approvals, cancellation, timeout, and kill switch."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from personal_assistant.core.planner import ExecutionPlan, PlanStep
from personal_assistant.core.registry import RiskLevel, ToolSpec, object_schema
from personal_assistant.core.results import ToolResult
from personal_assistant.core.task import StepState, TaskEngine, TaskState


def _waiting_tool(assistant, started: threading.Event | None = None):
    def handler(ctx, args):
        if started is not None:
            started.set()
        while True:
            ctx.check_cancel()
            time.sleep(0.01)

    assistant.registry.register(ToolSpec(
        name="test.wait",
        description="Wait until cancelled; test only.",
        risk=RiskLevel.READ_ONLY,
        category="test",
        schema=object_schema({"seconds": {"type": "number", "minimum": 0, "maximum": 60}}),
        handler=handler,
    ))
    spec = assistant.registry.get("test.wait")
    return ExecutionPlan(
        intent="test.wait",
        steps=[PlanStep(1, "test.wait", {}, "Wait", 1, False, spec)],
    )


def _broken_tool(assistant):
    def handler(ctx, args):
        return ToolResult.error("test.fail", "forced failure")

    assistant.registry.register(ToolSpec(
        name="test.fail",
        description="Fail on purpose; test only.",
        risk=RiskLevel.READ_ONLY,
        category="test",
        schema=object_schema({}),
        handler=handler,
    ))
    spec = assistant.registry.get("test.fail")
    return ExecutionPlan(
        intent="test.fail",
        steps=[PlanStep(1, "test.fail", {}, "Fail", 1, False, spec)],
    )


def test_successful_task_is_verified_and_persisted(make_assistant, workspace: Path) -> None:
    assistant, _ = make_assistant()
    response = assistant.handle_command("create a file task.txt in sandbox with content task data")
    assert response.ok
    assert response.outcome is not None
    assert response.outcome.state == TaskState.COMPLETED
    assert response.outcome.verified is True
    assert (workspace / "sandbox" / "task.txt").read_text(encoding="utf-8") == "task data"

    stored = assistant.memory.recent_tasks(1)[0]
    assert stored["state"] == "completed"
    assert stored["intent"] == "fs.write"
    assert stored["steps"][0]["tool"] == "fs.write"


def test_failed_tool_marks_task_failed(make_assistant) -> None:
    assistant, _ = make_assistant()
    outcome = assistant.tasks.execute(_broken_tool(assistant))
    assert outcome.state == TaskState.FAILED
    assert outcome.steps[0].state == StepState.FAILED
    assert "forced failure" in outcome.message


def test_sensitive_action_is_blocked_without_approval(make_assistant, workspace: Path) -> None:
    assistant, approver = make_assistant(approve=False)
    target = workspace / "sandbox" / "keep.txt"
    target.write_text("keep", encoding="utf-8")

    response = assistant.handle_command("delete keep.txt")
    assert response.outcome is not None
    assert response.outcome.state == TaskState.BLOCKED
    assert "approved" in response.text.lower()
    assert target.exists()
    assert approver.requests and approver.requests[0].risk == RiskLevel.SENSITIVE


def test_sensitive_action_runs_after_approval(make_assistant, workspace: Path) -> None:
    assistant, approver = make_assistant(approve=True)
    target = workspace / "sandbox" / "approved.txt"
    target.write_text("remove me", encoding="utf-8")

    response = assistant.handle_command("delete approved.txt")
    assert response.ok, response.text
    assert not target.exists()
    assert approver.requests
    assert response.outcome.undo_available is True


def test_kill_switch_blocks_all_actions_until_resumed(make_assistant, workspace: Path) -> None:
    assistant, _ = make_assistant()
    assistant.halt("testing")

    blocked = assistant.handle_command("create a file blocked.txt in sandbox with content no")
    assert blocked.kind == "error"
    assert blocked.outcome is None
    assert not (workspace / "sandbox" / "blocked.txt").exists()

    resumed = assistant.handle_command("resume")
    assert resumed.ok
    allowed = assistant.handle_command("create a file allowed.txt in sandbox with content yes")
    assert allowed.ok
    assert (workspace / "sandbox" / "allowed.txt").exists()


def test_task_cancellation_stops_running_tool(make_assistant) -> None:
    assistant, _ = make_assistant()
    started = threading.Event()
    plan = _waiting_tool(assistant, started)
    outcome_box: list = []

    def run():
        outcome_box.append(assistant.tasks.execute(plan))

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    assert started.wait(5), "the test tool did not start"
    assert assistant.cancel() >= 1
    worker.join(timeout=5)
    assert not worker.is_alive()
    outcome = outcome_box[0]
    assert outcome.state == TaskState.CANCELLED
    assert outcome.steps[0].state == StepState.CANCELLED


def test_task_timeout_cancels_and_reports(make_assistant) -> None:
    assistant, _ = make_assistant()
    assistant.config.set("app.max_task_seconds", 1)
    plan = _waiting_tool(assistant)
    started = time.monotonic()
    outcome = assistant.tasks.execute(plan)
    elapsed = time.monotonic() - started

    assert outcome.state == TaskState.CANCELLED
    assert outcome.timed_out is True
    assert 0.8 <= elapsed < 6
    assert "time limit" in (outcome.error + outcome.message).lower()


def test_plan_step_limit_is_enforced(make_assistant) -> None:
    assistant, _ = make_assistant()
    assistant.config.set("app.max_steps", 1)
    plan = _waiting_tool(assistant)
    second = PlanStep(2, "test.wait", {}, "Wait again", 1, False, plan.steps[0].spec)
    plan.steps.append(second)
    from personal_assistant.errors import ValidationError

    with pytest.raises(ValidationError, match="limit"):
        assistant.tasks.execute(plan)


def test_invalid_transition_is_rejected() -> None:
    assert TaskEngine._transition(TaskState.PENDING, TaskState.RUNNING) == TaskState.RUNNING
    with pytest.raises(RuntimeError, match="Invalid task state transition"):
        TaskEngine._transition(TaskState.COMPLETED, TaskState.RUNNING)
