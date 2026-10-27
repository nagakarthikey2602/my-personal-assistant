"""System information, process inspection, media keys, and audio device discovery."""

from __future__ import annotations

import ctypes
import os
import platform
import sys
import time
from typing import Any

import psutil

from ..core.context import ToolContext
from ..core.registry import RiskLevel, ToolSpec, object_schema
from ..core.results import ToolResult
from ..util import format_bytes

user32 = ctypes.WinDLL("user32", use_last_error=True)

_MEDIA_KEYS = {
    "play_pause": 0xB3,
    "next": 0xB0,
    "previous": 0xB1,
    "stop": 0xB2,
    "mute": 0xAD,
    "volume_up": 0xAF,
    "volume_down": 0xAE,
}


def register(registry) -> None:  # type: ignore[no-untyped-def]
    registry.register(ToolSpec(
        name="system.info",
        description="Report local hardware, operating system, Python, and assistant runtime status.",
        risk=RiskLevel.READ_ONLY,
        category="system",
        schema=object_schema({}),
        handler=_info,
    ))
    registry.register(ToolSpec(
        name="system.top_processes",
        description="Show processes consuming the most memory or CPU.",
        risk=RiskLevel.READ_ONLY,
        category="system",
        schema=object_schema({
            "sort": {"type": "string", "enum": ["memory", "cpu"], "default": "memory"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10},
        }),
        handler=_top_processes,
    ))
    registry.register(ToolSpec(
        name="system.media_control",
        description="Send a volume or media transport key and report that verification is unavailable.",
        risk=RiskLevel.REVERSIBLE,
        category="system",
        schema=object_schema({
            "action": {"type": "string", "enum": sorted(_MEDIA_KEYS.keys())},
        }, required=["action"]),
        handler=_media_control,
    ))
    registry.register(ToolSpec(
        name="system.battery",
        description="Report battery/power status.",
        risk=RiskLevel.READ_ONLY,
        category="system",
        schema=object_schema({}),
        handler=_battery,
    ))
    registry.register(ToolSpec(
        name="system.audio_devices",
        description="List microphone and speaker devices through the Windows audio API.",
        risk=RiskLevel.READ_ONLY,
        category="system",
        schema=object_schema({}),
        handler=_audio_devices,
    ))
    registry.register(ToolSpec(
        name="system.offline_status",
        description="Report application-level offline enforcement and blocked socket attempts.",
        risk=RiskLevel.READ_ONLY,
        category="system",
        schema=object_schema({}),
        handler=_offline_status,
    ))


def _info(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage(str(ctx.config.root))
    from ..offline_guard import status as offline_status

    data = {
        "os": platform.platform(),
        "windows_version": platform.version(),
        "architecture": platform.machine(),
        "processor": platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", ""),
        "logical_cpus": psutil.cpu_count(logical=True),
        "physical_cpus": psutil.cpu_count(logical=False),
        "memory_total": vm.total,
        "memory_available": vm.available,
        "memory_percent": vm.percent,
        "disk_total": disk.total,
        "disk_free": disk.free,
        "python": sys.version.split()[0],
        "app_root": str(ctx.config.root),
        "offline_mode": ctx.config.get("app.offline_mode"),
        "network_guard": offline_status(),
        "user": os.environ.get("USERNAME", ""),
    }
    return ToolResult.success(
        "system.info",
        f"{platform.system()} {platform.release()}, {psutil.cpu_count(logical=True)} logical CPUs, "
        f"{format_bytes(vm.total)} RAM, {format_bytes(disk.free)} free disk.",
        data=data,
    )


def _top_processes(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    sort_by = args.get("sort", "memory")
    limit = int(args.get("limit", 10))
    if sort_by == "cpu":
        for process in psutil.process_iter(["pid", "name"]):
            try:
                process.cpu_percent(None)
            except (psutil.Error, OSError):
                continue
        time.sleep(0.25)
    items: list[dict[str, Any]] = []
    for process in psutil.process_iter(["pid", "name", "memory_info", "status"]):
        ctx.check_cancel()
        try:
            info = process.info
            memory = info.get("memory_info")
            cpu = process.cpu_percent(None)
            items.append({
                "pid": info["pid"],
                "name": info.get("name") or "unknown",
                "memory_bytes": getattr(memory, "rss", 0) if memory else 0,
                "cpu_percent": round(cpu or 0.0, 1),
                "status": info.get("status"),
            })
        except (psutil.Error, OSError):
            continue
    key = "memory_bytes" if sort_by == "memory" else "cpu_percent"
    items.sort(key=lambda item: item[key], reverse=True)
    top = items[:limit]
    return ToolResult.success(
        "system.top_processes",
        f"Top {len(top)} processes by {sort_by}.",
        data={"sort": sort_by, "processes": top, "total_processes": len(items)},
    )


def _media_control(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    action = args["action"]
    vk = _MEDIA_KEYS[action]
    try:
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 2, 0)
    except Exception as exc:
        return ToolResult.error("system.media_control", f"Media key failed: {exc.__class__.__name__}")
    return ToolResult.unverified(
        "system.media_control",
        f"Sent the Windows media key '{action}'. Volume/media state cannot be read back with the current backend, so success is unverified.",
        data={"action": action, "verification": "unavailable"},
    )


def _battery(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    battery = psutil.sensors_battery()
    if battery is None:
        return ToolResult.success("system.battery", "No battery was detected (desktop or unknown power source).", data={"battery": None})
    seconds = None if battery.secsleft in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN) else battery.secsleft
    data = {"percent": round(battery.percent), "plugged_in": battery.power_plugged, "seconds_left": seconds}
    return ToolResult.success(
        "system.battery",
        f"Battery {data['percent']}% ({'plugged in' if battery.power_plugged else 'on battery'}).",
        data=data,
    )


def _audio_devices(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    try:
        from ..speech.native_audio import audio_status
    except ImportError as exc:
        return ToolResult.error("system.audio_devices", f"Audio device backend unavailable: {exc.__class__.__name__}")
    try:
        devices = audio_status()
    except Exception as exc:
        return ToolResult.error("system.audio_devices", f"Could not enumerate audio devices: {exc.__class__.__name__}")
    return ToolResult.success(
        "system.audio_devices",
        f"Found {len(devices['inputs'])} input and {len(devices['outputs'])} output device(s).",
        data={"devices": devices},
    )


def _offline_status(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from ..offline_guard import local_only_policy_note, status

    current = status()
    data = {
        "mode": ctx.config.get("app.offline_mode"),
        "guard": current,
        "note": local_only_policy_note(),
        "browser_network_enabled": ctx.config.get("tools.browser_network_enabled"),
    }
    return ToolResult.success(
        "system.offline_status",
        f"Offline mode is {data['mode']}; application network guard installed={current['installed']}.",
        data=data,
    )
