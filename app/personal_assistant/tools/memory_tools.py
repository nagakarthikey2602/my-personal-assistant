"""Explicit memory preferences, inspection, export, and deletion."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.context import ToolContext
from ..core.registry import RiskLevel, ToolSpec, object_schema
from ..core.results import ToolResult
from ..util import new_id, sha256_file


def register(registry) -> None:  # type: ignore[no-untyped-def]
    registry.register(ToolSpec(
        name="memory.remember",
        description="Store an explicit user preference in the local SQLite database.",
        risk=RiskLevel.REVERSIBLE,
        category="memory",
        schema=object_schema({
            "key": {"type": "string", "minLength": 1, "maxLength": 100, "pattern": r"[A-Za-z0-9 _.-]+"},
            "value": {"type": "string", "minLength": 1, "maxLength": 5000},
        }, required=["key", "value"]),
        handler=_remember,
    ))
    registry.register(ToolSpec(
        name="memory.list",
        description="List stored preferences and recent task history.",
        risk=RiskLevel.READ_ONLY,
        category="memory",
        schema=object_schema({
            "history_limit": {"type": "integer", "minimum": 0, "maximum": 50, "default": 5},
        }),
        handler=_list,
    ))
    registry.register(ToolSpec(
        name="memory.forget",
        description="Delete a preference, all preferences, or all memory records including task history.",
        risk=RiskLevel.SENSITIVE,
        category="memory",
        dynamic_risk=lambda args: RiskLevel.SENSITIVE if args.get("all") else RiskLevel.REVERSIBLE,
        schema=object_schema({
            "key": {"type": "string", "minLength": 1, "maxLength": 100},
            "all": {"type": "boolean", "default": False},
            "include_training": {"type": "boolean", "default": False},
        }),
        handler=_forget,
    ))
    registry.register(ToolSpec(
        name="memory.status",
        description="Report memory storage, retention, and row counts.",
        risk=RiskLevel.READ_ONLY,
        category="memory",
        schema=object_schema({}),
        handler=_status,
    ))
    registry.register(ToolSpec(
        name="memory.export",
        description="Export preferences, task history, and training examples to a local JSON file.",
        risk=RiskLevel.REVERSIBLE,
        category="memory",
        undoable=True,
        schema=object_schema({
            "path": {"type": "string", "minLength": 1, "maxLength": 30000},
        }, required=["path"]),
        handler=_export,
    ))
    registry.register(ToolSpec(
        name="memory.set_enabled",
        description="Enable or disable all local memory storage without deleting existing records.",
        risk=RiskLevel.REVERSIBLE,
        category="memory",
        schema=object_schema({
            "enabled": {"type": "boolean"},
        }, required=["enabled"]),
        handler=_set_enabled,
    ))
    registry.register(ToolSpec(
        name="memory.add_example",
        description="Add a user-approved command example for future intent-model training (not a preference).",
        risk=RiskLevel.REVERSIBLE,
        category="memory",
        schema=object_schema({
            "text": {"type": "string", "minLength": 1, "maxLength": 500},
            "label": {"type": "string", "minLength": 1, "maxLength": 100},
            "approved": {"type": "boolean", "default": False},
        }, required=["text", "label"]),
        handler=_add_example,
    ))


def _store(ctx: ToolContext):
    if ctx.memory is None:
        raise RuntimeError("Memory service is unavailable.")
    return ctx.memory


def _remember(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    store = _store(ctx)
    if not store.enabled:
        return ToolResult.error("memory.remember", "Memory is disabled in configuration.")
    store.set_preference(args["key"], args["value"])
    return ToolResult.success(
        "memory.remember",
        f"Stored preference '{args['key']}' locally.",
        data={"key": args["key"], "value_preview": args["value"][:120]},
    )


def _list(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    store = _store(ctx)
    if not store.enabled:
        return ToolResult.error("memory.list", "Memory is disabled in configuration.")
    preferences = store.list_preferences()
    history_limit = int(args.get("history_limit", 5))
    tasks = store.recent_tasks(history_limit) if history_limit else []
    return ToolResult.success(
        "memory.list",
        f"{len(preferences)} preference(s) and {len(tasks)} recent task(s).",
        data={"preferences": preferences, "recent_tasks": tasks},
    )


def _forget(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    store = _store(ctx)
    if not store.enabled:
        return ToolResult.error("memory.forget", "Memory is disabled in configuration.")
    if args.get("all"):
        removed = store.forget_all(include_training=bool(args.get("include_training", False)))
        return ToolResult.success(
            "memory.forget",
            "Deleted all local task history and preferences"
            + (" and training examples" if args.get("include_training") else "")
            + ".",
            data={"removed": removed},
        )
    key = args.get("key")
    if not key:
        return ToolResult.error("memory.forget", "Provide a preference key or set all=true.")
    count = store.delete_preference(key)
    return ToolResult.success(
        "memory.forget",
        f"Deleted {count} preference record(s) for '{key}'.",
        data={"key": key, "removed": count},
    )


def _status(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    store = _store(ctx)
    status = store.status()
    return ToolResult.success(
        "memory.status",
        "Memory is enabled." if status.get("enabled") else "Memory is disabled.",
        data=status,
    )


def _export(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    store = _store(ctx)
    if not store.enabled:
        return ToolResult.error("memory.export", "Memory is disabled in configuration.")
    destination = ctx.path_policy.check_write(args["path"])
    if destination.exists():
        return ToolResult.error("memory.export", "The export destination already exists.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    store.export_all(destination)
    try:
        parsed = json.loads(destination.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as exc:
        return ToolResult.error("memory.export", f"Export verification failed: {exc}")
    undo_id = ctx.recovery.record(
        description=f"Export memory {destination.name}",
        tool="memory.export",
        task_id=ctx.task_id,
        actions=[{"op": "quarantine_path", "path": str(destination), "sha256": sha256_file(destination)}],
    )
    return ToolResult.success(
        "memory.export",
        f"Exported local memory data to {destination.name}.",
        data={
            "path": str(destination),
            "bytes": destination.stat().st_size,
            "warning": "This file contains personal data. Store it privately.",
            "undo_id": undo_id,
        },
    )


def _set_enabled(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    store = _store(ctx)
    enabled = bool(args["enabled"])
    store.set_enabled(enabled)
    return ToolResult.success(
        "memory.set_enabled",
        "Local memory is now enabled." if enabled else "Local memory is now disabled; existing records were kept.",
        data={"enabled": enabled},
    )


def _add_example(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    store = _store(ctx)
    if not store.enabled:
        return ToolResult.error("memory.add_example", "Memory is disabled in configuration.")
    example_id = store.add_training_example(
        args["text"],
        args["label"],
        source="user_tool",
        approved=bool(args.get("approved", False)),
    )
    return ToolResult.success(
        "memory.add_example",
        "Added a local training example. It is separate from preferences and is not used until a training run.",
        data={"id": example_id, "approved": bool(args.get("approved", False))},
    )
