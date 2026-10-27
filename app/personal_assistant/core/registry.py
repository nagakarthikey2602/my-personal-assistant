"""Explicit tool registry. Every executable action must resolve through this table."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, TYPE_CHECKING

from ..errors import ToolUnavailable, ValidationError
from ..util import redact
from .schema import validate, validate_schema_definition

if TYPE_CHECKING:
    from .context import ToolContext
    from .results import ToolResult


class RiskLevel:
    READ_ONLY = 1
    REVERSIBLE = 2
    SENSITIVE = 3
    PRIVILEGED = 4

    LABELS = {
        1: "read-only",
        2: "reversible",
        3: "sensitive",
        4: "privileged/high-impact",
    }

    @classmethod
    def label(cls, level: int) -> str:
        return cls.LABELS.get(int(level), f"unknown({level})")


@dataclass
class ToolSpec:
    name: str
    description: str
    risk: int
    schema: dict[str, Any]
    handler: Callable[..., "ToolResult"]
    category: str = "general"
    requires_network: bool = False
    undoable: bool = False
    dynamic_risk: Callable[[dict[str, Any]], int] | None = None
    enabled_setting: str | None = None

    def effective_risk(self, args: dict[str, Any]) -> int:
        if self.dynamic_risk is not None:
            try:
                value = int(self.dynamic_risk(args))
            except Exception:
                return self.risk
            return max(1, min(4, value))
        return int(self.risk)

    def catalog_entry(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "risk": int(self.risk),
            "risk_label": RiskLevel.label(self.risk),
            "category": self.category,
            "requires_network": self.requires_network,
            "undoable": self.undoable,
            "schema": redact(self.schema, max_len=300),
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if not spec.name or not spec.name.replace("_", "").replace(".", "").isalnum():
            raise ValueError(f"Invalid tool name: {spec.name!r}")
        if spec.name in self._tools:
            raise ValueError(f"Duplicate tool: {spec.name}")
        if spec.risk not in (1, 2, 3, 4):
            raise ValueError(f"Invalid risk level for {spec.name}")
        if not isinstance(spec.schema, dict) or spec.schema.get("type") != "object":
            raise ValueError(f"Tool {spec.name} needs an object schema")
        validate_schema_definition(spec.schema)
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolUnavailable(f"Unknown tool: {name}. Only registered tools may run.") from exc

    def has(self, name: str) -> bool:
        return name in self._tools

    def all(self) -> list[ToolSpec]:
        return [self._tools[key] for key in sorted(self._tools)]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def catalog(self) -> list[dict[str, Any]]:
        return [spec.catalog_entry() for spec in self.all()]

    def validate_args(self, name: str, args: dict[str, Any]) -> None:
        spec = self.get(name)
        if not isinstance(args, dict):
            raise ValidationError("Tool arguments must be an object.")
        try:
            validate(spec.schema, args)
        except ValidationError as exc:
            raise ValidationError(f"{name}: {exc}") from exc


def object_schema(
    properties: dict[str, Any],
    *,
    required: list[str] | None = None,
    description: str = "",
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }
    if description:
        schema["description"] = description
    return schema
