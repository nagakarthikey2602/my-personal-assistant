"""Small, strict JSON-Schema subset used to validate tool arguments.

The assistant does not need a general schema engine; it needs predictable
validation with no silent coercion. Only the keywords below are accepted.
"""

from __future__ import annotations

import re
from typing import Any

from ..errors import ValidationError

_ALLOWED_KEYWORDS = {
    "type", "description", "title", "default", "enum", "required", "properties",
    "additionalProperties", "items", "minItems", "maxItems", "minimum", "maximum",
    "minLength", "maxLength", "pattern", "format",
}

_TYPE_NAMES = {"object", "array", "string", "integer", "number", "boolean", "null"}


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def _validate(schema: dict[str, Any], value: Any, path: str) -> None:
    unknown = set(schema) - _ALLOWED_KEYWORDS
    if unknown:
        raise ValidationError(f"Tool schema uses unsupported keywords at {path or 'root'}: {sorted(unknown)}")
    expected = schema.get("type")
    if expected:
        if isinstance(expected, list):
            if not any(_type_ok(value, item) for item in expected):
                raise ValidationError(f"{path or 'value'} must be one of {expected}.")
        elif expected not in _TYPE_NAMES:
            raise ValidationError(f"Invalid schema type '{expected}'.")
        elif not _type_ok(value, expected):
            raise ValidationError(f"{path or 'value'} must be {expected}.")
    if "enum" in schema and value not in schema["enum"]:
        raise ValidationError(f"{path or 'value'} must be one of {schema['enum']}.")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            raise ValidationError(f"{path or 'value'} is too short.")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            raise ValidationError(f"{path or 'value'} is too long.")
        if "pattern" in schema and re.fullmatch(str(schema["pattern"]), value) is None:
            raise ValidationError(f"{path or 'value'} has an invalid format.")
        if schema.get("format") == "uri":
            if not re.match(r"^(https?|file)://", value, flags=re.I):
                raise ValidationError(f"{path or 'value'} must be an allowed URI.")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValidationError(f"{path or 'value'} must be >= {schema['minimum']}.")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValidationError(f"{path or 'value'} must be <= {schema['maximum']}.")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            raise ValidationError(f"{path or 'value'} needs at least {schema['minItems']} item(s).")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            raise ValidationError(f"{path or 'value'} has too many items.")
        if "items" in schema:
            for index, item in enumerate(value):
                _validate(schema["items"], item, f"{path}[{index}]")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                raise ValidationError(f"Missing required argument: {path + '.' if path else ''}{key}")
        additional = schema.get("additionalProperties", False)
        for key, item in value.items():
            if key in properties:
                _validate(properties[key], item, f"{path + '.' if path else ''}{key}")
            elif additional is True:
                continue
            else:
                raise ValidationError(f"Unknown argument: {path + '.' if path else ''}{key}")


def validate(schema: dict[str, Any], value: Any) -> None:
    if not isinstance(schema, dict):
        raise ValidationError("Tool schema must be an object.")
    _validate(schema, value, "")


def validate_schema_definition(schema: dict[str, Any]) -> None:
    """Validate the tool schema itself without requiring argument values."""
    if not isinstance(schema, dict):
        raise ValidationError("Tool schema must be an object.")
    unknown = set(schema) - _ALLOWED_KEYWORDS
    if unknown:
        raise ValidationError(f"Tool schema uses unsupported keywords: {sorted(unknown)}")
    if schema.get("type") != "object":
        raise ValidationError("A tool schema must describe an object.")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise ValidationError("Tool schema properties must be an object.")
    for name, child in properties.items():
        if not isinstance(name, str) or not isinstance(child, dict):
            raise ValidationError("Each tool property needs a name and a schema object.")
        unknown = set(child) - _ALLOWED_KEYWORDS
        if unknown:
            raise ValidationError(f"Property {name} uses unsupported keywords: {sorted(unknown)}")
        child_type = child.get("type")
        if child_type and child_type not in _TYPE_NAMES and not isinstance(child_type, list):
            raise ValidationError(f"Property {name} has an invalid type.")
        if child.get("type") == "object":
            validate_schema_definition(child)
        if child.get("type") == "array" and isinstance(child.get("items"), dict):
            unknown_items = set(child["items"]) - _ALLOWED_KEYWORDS
            if unknown_items:
                raise ValidationError(f"Property {name} items use unsupported keywords.")
    required = schema.get("required", [])
    if not isinstance(required, list) or any(item not in properties for item in required):
        raise ValidationError("Tool schema required entries must refer to declared properties.")
