"""Read-mostly Windows integration: display, network, power, startup, settings pages."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import psutil

from ..core.context import ToolContext
from ..core.registry import RiskLevel, ToolSpec, object_schema
from ..core.results import ToolResult
from .applications import enumerate_windows, focus_window_by_title

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
user32.GetSystemMetrics.restype = ctypes.c_int
try:
    user32.GetDpiForSystem.restype = wintypes.UINT
except AttributeError:
    pass

MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
user32.EnumDisplayMonitors.argtypes = (wintypes.HDC, ctypes.c_void_p, MONITORENUMPROC, wintypes.LPARAM)

SETTINGS_PAGES = {
    "home": "ms-settings:",
    "display": "ms-settings:display",
    "sound": "ms-settings:sound",
    "bluetooth": "ms-settings:bluetooth",
    "network": "ms-settings:network",
    "wifi": "ms-settings:network-wifi",
    "power": "ms-settings:powersleep",
    "notifications": "ms-settings:notifications",
    "apps": "ms-settings:appsfeatures",
    "startup": "ms-settings:startupapps",
    "accessibility": "ms-settings:easeofaccess",
    "about": "ms-settings:about",
    "privacy": "ms-settings:privacy",
}

CREATE_NO_WINDOW = 0x08000000


def register(registry) -> None:  # type: ignore[no-untyped-def]
    registry.register(ToolSpec(
        name="windows.display_info",
        description="Report display resolution, monitor count, and DPI.",
        risk=RiskLevel.READ_ONLY,
        category="windows",
        schema=object_schema({}),
        handler=_display_info,
    ))
    registry.register(ToolSpec(
        name="windows.network_status",
        description="Report network interface state and Wi-Fi connection details (read-only).",
        risk=RiskLevel.READ_ONLY,
        category="windows",
        schema=object_schema({}),
        handler=_network_status,
    ))
    registry.register(ToolSpec(
        name="windows.bluetooth_status",
        description="Report the Bluetooth support service state (read-only).",
        risk=RiskLevel.READ_ONLY,
        category="windows",
        schema=object_schema({}),
        handler=_bluetooth_status,
    ))
    registry.register(ToolSpec(
        name="windows.power_status",
        description="Report the active power plan, battery state, and sleep availability.",
        risk=RiskLevel.READ_ONLY,
        category="windows",
        schema=object_schema({}),
        handler=_power_status,
    ))
    registry.register(ToolSpec(
        name="windows.startup_list",
        description="List per-user and machine startup entries from the registry (read-only).",
        risk=RiskLevel.READ_ONLY,
        category="windows",
        schema=object_schema({}),
        handler=_startup_list,
    ))
    registry.register(ToolSpec(
        name="windows.open_settings",
        description="Open a specific Windows Settings page without changing any setting.",
        risk=RiskLevel.REVERSIBLE,
        category="windows",
        schema=object_schema({
            "page": {"type": "string", "enum": sorted(SETTINGS_PAGES.keys())},
        }, required=["page"]),
        handler=_open_settings,
    ))
    registry.register(ToolSpec(
        name="windows.open_folder",
        description="Open a folder in File Explorer and verify a window appeared.",
        risk=RiskLevel.REVERSIBLE,
        category="windows",
        schema=object_schema({"path": {"type": "string", "minLength": 1, "maxLength": 30000}}, required=["path"]),
        handler=_open_folder,
    ))


def _fixed_command(args: list[str], timeout: int = 15) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            creationflags=CREATE_NO_WINDOW,
            encoding="utf-8",
            errors="replace",
        )
        return {
            "ok": completed.returncode == 0,
            "code": completed.returncode,
            "output": (completed.stdout or "") + (completed.stderr or ""),
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "code": -1, "output": f"{exc.__class__.__name__}: {exc}"}


def _display_info(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    monitors: list[dict[str, int]] = []

    def callback(hmonitor, hdc, rect, lparam):  # type: ignore[no-untyped-def]
        monitors.append({"left": rect.contents.left, "top": rect.contents.top, "right": rect.contents.right, "bottom": rect.contents.bottom})
        return True

    try:
        user32.EnumDisplayMonitors(0, None, MONITORENUMPROC(callback), 0)
        dpi = int(user32.GetDpiForSystem()) if hasattr(user32, "GetDpiForSystem") else 96
        data = {
            "primary_width": user32.GetSystemMetrics(0),
            "primary_height": user32.GetSystemMetrics(1),
            "virtual_width": user32.GetSystemMetrics(78),
            "virtual_height": user32.GetSystemMetrics(79),
            "monitor_count": len(monitors) or 1,
            "monitors": monitors,
            "dpi": dpi,
            "scale_percent": round(dpi / 96 * 100),
            "note": "Changing display settings is not implemented; this tool is read-only.",
        }
    except Exception as exc:
        return ToolResult.error("windows.display_info", f"Display information failed: {exc.__class__.__name__}")
    return ToolResult.success("windows.display_info", f"{data['monitor_count']} monitor(s), primary {data['primary_width']}x{data['primary_height']}.", data=data)


def _network_status(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    interfaces = []
    for name, stats in psutil.net_if_stats().items():
        addresses = []
        try:
            for address in psutil.net_if_addrs().get(name, []):
                if address.family.name in {"AF_INET", "AF_INET6"}:
                    addresses.append(address.address)
        except Exception:
            pass
        interfaces.append({"name": name, "up": stats.isup, "speed_mbps": stats.speed, "addresses": addresses})
    wifi: dict[str, str] = {}
    result = _fixed_command(["netsh", "wlan", "show", "interfaces"], timeout=10)
    if result["ok"]:
        for line in result["output"].splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                key = key.strip()
                if key in {"Name", "State", "SSID", "Signal", "Authentication", "Radio status"}:
                    wifi[key.lower().replace(" ", "_")] = value.strip()
    return ToolResult.success(
        "windows.network_status",
        f"{sum(1 for item in interfaces if item['up'])} interface(s) up."
        + (f" Wi-Fi state: {wifi.get('state', 'unknown')}." if wifi else " No Wi-Fi interface reported."),
        data={"interfaces": interfaces, "wifi": wifi, "note": "Network settings are not changed by this tool."},
    )


def _bluetooth_status(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    result = _fixed_command(["sc", "query", "bthserv"], timeout=10)
    text = result["output"]
    state = "unknown"
    match = re.search(r"STATE\s*:\s*(\d+)\s*(\w+)", text)
    if match:
        state = match.group(2)
    return ToolResult.success(
        "windows.bluetooth_status",
        f"Bluetooth support service state: {state}. Adapter and pairing management are not implemented.",
        data={"service_state": state, "command_available": result["ok"], "note": "Read-only status from sc.exe."},
    )


def _power_status(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    scheme = _fixed_command(["powercfg", "/getactivescheme"], timeout=10)
    battery = None
    try:
        info = psutil.sensors_battery()
        if info:
            battery = {
                "percent": round(info.percent),
                "plugged_in": info.power_plugged,
                "seconds_left": info.secsleft if info.secsleft != psutil.POWER_TIME_UNLIMITED else None,
            }
    except Exception:
        pass
    text = scheme["output"].strip().splitlines()[-1] if scheme["ok"] and scheme["output"].strip() else "unknown"
    return ToolResult.success(
        "windows.power_status",
        f"Active power plan: {text}",
        data={"power_plan": text, "battery": battery, "changing_power_settings": "not implemented"},
    )


def _startup_list(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    import winreg

    entries: list[dict[str, str]] = []
    locations = [
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", "HKCU Run"),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\RunOnce", "HKCU RunOnce"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run", "HKLM Run"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\RunOnce", "HKLM RunOnce"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run", "HKLM Run (32-bit)"),
    ]
    for hive, subkey, label in locations:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                count = winreg.QueryInfoKey(key)[1]
                for index in range(count):
                    try:
                        name, value, _ = winreg.EnumValue(key, index)
                        entries.append({"name": name, "command": str(value), "location": label})
                    except OSError:
                        continue
        except OSError:
            continue
    return ToolResult.success(
        "windows.startup_list",
        f"Found {len(entries)} startup registry entries. This tool does not modify them.",
        data={"entries": entries, "note": "Startup approval changes are not implemented."},
    )


def _open_settings(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    page = args["page"]
    uri = SETTINGS_PAGES[page]
    before = {window["hwnd"] for window in enumerate_windows()}
    try:
        os.startfile(uri)  # type: ignore[attr-defined]
    except OSError as exc:
        return ToolResult.error("windows.open_settings", f"Could not open Settings: {exc.__class__.__name__}")
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        ctx.check_cancel()
        time.sleep(0.3)
        windows = enumerate_windows()
        for window in windows:
            title = window["title"].lower()
            process = (window["process"] or "").lower()
            if "settings" in title or "systemsettings" in process:
                # An already-open Settings window can be re-used; that still
                # supports opening the requested page, but we only claim
                # verification when a new window was actually observed.
                is_new = window["hwnd"] not in before
                if is_new:
                    return ToolResult.success(
                        "windows.open_settings",
                        f"Opened the Windows Settings page '{page}'. No setting was changed.",
                        data={"uri": uri, "window": window["title"], "verified": True},
                    )
                return ToolResult.unverified(
                    "windows.open_settings",
                    f"Requested the Settings page '{page}'; an existing Settings window was already open. "
                    "No setting was changed and the page state was not verified.",
                    data={"uri": uri, "window": window["title"], "verified": False},
                )
    return ToolResult.unverified(
        "windows.open_settings",
        f"Requested the Settings page '{page}', but no Settings window was observed. No setting was changed.",
        data={"uri": uri},
    )


def _open_folder(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    folder = ctx.path_policy.check_read(args["path"])
    if not folder.is_dir():
        return ToolResult.error("windows.open_folder", "The folder does not exist.")
    before = {window["hwnd"] for window in enumerate_windows()}
    try:
        subprocess.Popen(
            ["explorer.exe", str(folder)],
            shell=False,
            close_fds=True,
            creationflags=CREATE_NO_WINDOW,
        )
    except OSError as exc:
        return ToolResult.error("windows.open_folder", f"Could not open File Explorer: {exc.__class__.__name__}")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        ctx.check_cancel()
        time.sleep(0.25)
        new_windows = [window for window in enumerate_windows() if window["hwnd"] not in before]
        for window in new_windows:
            if folder.name.lower() in window["title"].lower() or (window["process"] or "").lower() == "explorer.exe":
                return ToolResult.success(
                    "windows.open_folder",
                    f"Opened {folder} in File Explorer.",
                    data={"path": str(folder), "window": window["title"]},
                )
    return ToolResult.unverified(
        "windows.open_folder",
        f"Requested File Explorer for {folder}, but no new window was observed.",
        data={"path": str(folder)},
    )
