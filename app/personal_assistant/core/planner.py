"""Turn a parsed request into a validated, risk-annotated execution plan.

The planner is the bridge between natural language and the tool registry. It
never invents tools and never weakens a schema: any unknown tool or invalid
argument fails before execution.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from ..config import Config
from ..errors import ValidationError
from .parser import ParseResult
from .registry import RiskLevel, ToolRegistry, ToolSpec


@dataclass
class PlanStep:
    index: int
    tool: str
    args: dict[str, Any]
    title: str
    risk: int
    undoable: bool
    spec: ToolSpec = field(repr=False, compare=False)


@dataclass
class ExecutionPlan:
    intent: str
    steps: list[PlanStep]
    source: str = "rules"
    confidence: float = 1.0
    notes: str = ""

    @property
    def highest_risk(self) -> int:
        return max((step.risk for step in self.steps), default=0)

    def summary(self) -> str:
        if len(self.steps) == 1:
            return self.steps[0].title
        return f"{len(self.steps)} steps: " + ", ".join(step.title for step in self.steps[:4]) + (
            "…" if len(self.steps) > 4 else ""
        )


class TaskPlanner:
    def __init__(self, registry: ToolRegistry, config: Config) -> None:
        self.registry = registry
        self.config = config

    def build(self, parsed: ParseResult) -> ExecutionPlan:
        if parsed.kind != "plan" or not parsed.steps:
            raise ValidationError("There is no action plan to execute.")
        max_steps = int(self.config.get("app.max_steps", 40))
        if len(parsed.steps) > max_steps:
            raise ValidationError(
                f"The plan has {len(parsed.steps)} steps, above the configured limit of {max_steps}."
            )
        steps: list[PlanStep] = []
        for index, raw in enumerate(parsed.steps, start=1):
            spec = self.registry.get(raw.tool)
            args = copy.deepcopy(raw.args)
            self.registry.validate_args(raw.tool, args)
            risk = spec.effective_risk(args)
            title = raw.title or spec.description
            steps.append(
                PlanStep(
                    index=index,
                    tool=raw.tool,
                    args=args,
                    title=str(title)[:300],
                    risk=risk,
                    undoable=spec.undoable,
                    spec=spec,
                )
            )
        return ExecutionPlan(
            intent=parsed.intent,
            steps=steps,
            source=parsed.source,
            confidence=parsed.confidence,
            notes=parsed.message,
        )

    @staticmethod
    def describe_risks(steps: list[PlanStep]) -> str:
        if not steps:
            return "No actions."
        labels = [f"{step.title} ({RiskLevel.label(step.risk)})" for step in steps]
        return "; ".join(labels)
