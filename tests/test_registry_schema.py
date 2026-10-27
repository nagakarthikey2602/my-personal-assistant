"""Strict schema validation, registry invariants, and argument rejection."""

from __future__ import annotations

import pytest

from personal_assistant.core.registry import RiskLevel, ToolRegistry, ToolSpec, object_schema
from personal_assistant.core.results import ToolResult
from personal_assistant.core.schema import validate, validate_schema_definition
from personal_assistant.errors import ToolUnavailable, ValidationError


def _spec(name: str = "test.echo", **overrides) -> ToolSpec:
    values = {
        "name": name,
        "description": "Echo a string.",
        "risk": RiskLevel.READ_ONLY,
        "schema": object_schema({"text": {"type": "string", "minLength": 1}}, required=["text"]),
        "handler": lambda ctx, args: ToolResult.success(name, args["text"]),
    }
    values.update(overrides)
    return ToolSpec(**values)


def test_unknown_and_missing_arguments_are_rejected() -> None:
    schema = object_schema({"text": {"type": "string"}}, required=["text"])
    with pytest.raises(ValidationError, match="Missing required"):
        validate(schema, {})
    with pytest.raises(ValidationError, match="Unknown argument"):
        validate(schema, {"text": "ok", "extra": True})
    with pytest.raises(ValidationError, match="must be string"):
        validate(schema, {"text": 4})


def test_bounds_enum_and_boolean_are_strict() -> None:
    schema = object_schema({
        "count": {"type": "integer", "minimum": 1, "maximum": 3},
        "mode": {"type": "string", "enum": ["a", "b"]},
        "flag": {"type": "boolean"},
    })
    validate(schema, {"count": 2, "mode": "a", "flag": False})
    with pytest.raises(ValidationError):
        validate(schema, {"count": 0})
    with pytest.raises(ValidationError):
        validate(schema, {"count": 4})
    with pytest.raises(ValidationError):
        validate(schema, {"mode": "c"})
    with pytest.raises(ValidationError):
        validate(schema, {"count": True})
    with pytest.raises(ValidationError):
        validate(schema, {"flag": 1})


def test_schema_definition_rejects_unsupported_keywords() -> None:
    with pytest.raises(ValidationError):
        validate_schema_definition({"type": "object", "oneOf": []})
    with pytest.raises(ValidationError):
        validate_schema_definition({"type": "string"})


def test_registry_rejects_duplicate_and_malformed_tools() -> None:
    registry = ToolRegistry()
    registry.register(_spec())
    with pytest.raises(ValueError, match="Duplicate"):
        registry.register(_spec())
    with pytest.raises(ValueError, match="Invalid tool name"):
        registry.register(_spec("bad name"))
    with pytest.raises(ValueError, match="risk"):
        registry.register(_spec("test.risk", risk=7))
    with pytest.raises(ValidationError):
        registry.register(_spec("test.schema", schema={"type": "object", "bogus": 1}))


def test_registry_unknown_tool_and_argument_validation() -> None:
    registry = ToolRegistry()
    registry.register(_spec())
    with pytest.raises(ToolUnavailable):
        registry.get("missing.tool")
    with pytest.raises(ValidationError, match="test.echo"):
        registry.validate_args("test.echo", {})


def test_dynamic_risk_is_clamped() -> None:
    spec = _spec(dynamic_risk=lambda args: 9)
    assert spec.effective_risk({}) == 4
    fallback = _spec(dynamic_risk=lambda args: (_ for _ in ()).throw(RuntimeError()))
    assert fallback.effective_risk({}) == RiskLevel.READ_ONLY
