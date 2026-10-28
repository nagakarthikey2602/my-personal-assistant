"""Windows application discovery, launching, window control, and safe typing."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import fnmatch
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import psutil

from ..core.context import ToolContext
from ..core.registry import RiskLevel, ToolSpec, object_schema
from ..core.results import ToolResult
from ..errors import ToolUnavailable, ValidationError
from ..util import human_join

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = (WNDENUMPROC, wintypes.LPARAM)
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = (wintypes.HWND,)
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
user32.ShowWindow.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = (wintypes.HWND,)
user32.BringWindowToTop.restype = wintypes.BOOL
user32.IsWindow.argtypes = (wintypes.HWND,)
user32.IsWindow.restype = wintypes.BOOL
user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
user32.GetWindowLongW.restype = wintypes.LONG
user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
user32.AttachThreadInput.restype = wintypes.BOOL

SW_RESTORE = 9
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WM_CLOSE = 0x0010

KNOWN_APPS: dict[str, dict[str, Any]] = {
    "notepad": {
        "title": "Notepad",
        "aliases": ["notepad", "text editor"],
        "launch": "notepad.exe",
        "capabilities": ["launch", "type_text", "focus", "close"],
        "notes": "Typing is sent as Unicode keystrokes. Verification of text content is not available without UIA.",
    },
    "calculator": {
        "title": "Calculator",
        "aliases": ["calculator", "calc"],
        "uri": "calculator:",
        "capabilities": ["launch", "focus", "close"],
        "notes": "Windows 11 Calculator is a packaged app; launching is verified by process/window observation.",
    },
    "paint": {
        "title": "Paint",
        "aliases": ["paint", "mspaint"],
        "launch": "mspaint.exe",
        "capabilities": ["launch", "focus", "close"],
        "notes": "No drawing-specific automation is implemented.",
    },
    "explorer": {
        "title": "File Explorer",
        "aliases": ["file explorer", "explorer", "files"],
        "launch": "explorer.exe",
        "capabilities": ["launch", "focus", "close"],
        "notes": "Window interaction beyond focus is not implemented.",
    },
    "settings": {
        "title": "Settings",
        "aliases": ["settings", "windows settings"],
        "uri": "ms-settings:",
        "capabilities": ["launch", "focus"],
        "notes": "A few settings pages may be opened by URI in a future milestone; no setting is changed automatically.",
    },
}

_SAFE_TYPE_PROCESSES = {"notepad.exe", "wordpad.exe", "notepad++.exe"}
_cached_shortcuts: tuple[float, list[dict[str, str]]] | None = None


def register(registry) -> None:  # type: ignore[no-untyped-def]
    registry.register(ToolSpec(
        name="apps.list_installed",
        description="List applications visible in Start Menu shortcuts and registered App Paths.",
        risk=RiskLevel.READ_ONLY,
        category="applications",
        schema=object_schema({}),
        handler=_list_installed,
    ))
    registry.register(ToolSpec(
        name="apps.capabilities",
        description="Show the application-specific capability registry.",
        risk=RiskLevel.READ_ONLY,
        category="applications",
        schema=object_schema({}),
        handler=_capabilities,
    ))
    registry.register(ToolSpec(
        name="apps.launch",
        description="Launch a known or installed application and verify it started.",
        risk=RiskLevel.REVERSIBLE,
        category="applications",
        schema=object_schema({
            "app": {"type": "string", "minLength": 1, "maxLength": 200},
        }, required=["app"]),
        handler=_launch,
    ))
    registry.register(ToolSpec(
        name="apps.list_windows",
        description="List visible top-level windows with titles and owning processes.",
        risk=RiskLevel.READ_ONLY,
        category="applications",
        schema=object_schema({}),
        handler=_list_windows,
    ))
    registry.register(ToolSpec(
        name="apps.focus_window",
        description="Bring a visible window to the foreground by exact or partial title.",
        risk=RiskLevel.REVERSIBLE,
        category="applications",
        schema=object_schema({
            "title": {"type": "string", "minLength": 1, "maxLength": 500},
            "exact": {"type": "boolean", "default": False},
        }, required=["title"]),
        handler=_focus_window,
    ))
    registry.register(ToolSpec(
        name="apps.type_text",
        description="Type Unicode text into the foreground window; verification of content is not available.",
        risk=RiskLevel.REVERSIBLE,
        category="applications",
        dynamic_risk=lambda args: _type_risk(args),
        schema=object_schema({
            "text": {"type": "string", "minLength": 1, "maxLength": 10000},
            "window_title": {"type": "string", "maxLength": 500},
        }, required=["text"]),
        handler=_type_text,
    ))
    registry.register(ToolSpec(
        name="apps.close",
        description="Request a graceful close of exactly one running application; force-kill is privileged.",
        risk=RiskLevel.SENSITIVE,
        category="applications",
        dynamic_risk=lambda args: RiskLevel.PRIVILEGED if args.get("force") else RiskLevel.SENSITIVE,
        schema=object_schema({
            "name": {"type": "string", "minLength": 1, "maxLength": 200},
            "force": {"type": "boolean", "default": False},
        }, required=["name"]),
        handler=_close,
    ))


# Window API helpers ------------------------------------------------------
def _window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def enumerate_windows() -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []

    def callback(hwnd: int, lparam: int) -> bool:
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            exstyle = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if exstyle & WS_EX_TOOLWINDOW:
                return True
            title = _window_title(hwnd)
            if not title.strip():
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            process_name = ""
            try:
                process_name = psutil.Process(pid.value).name()
            except (psutil.Error, ValueError):
                pass
            windows.append({
                "hwnd": int(hwnd),
                "title": title,
                "pid": int(pid.value),
                "process": process_name,
                "rect": [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)],
            })
        except Exception:
            return True
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return windows


def _find_window(title: str, exact: bool = False) -> dict[str, Any] | None:
    matches = [
        window for window in enumerate_windows()
        if (window["title"] == title if exact else title.lower() in window["title"].lower())
    ]
    if not matches:
        return None
    matches.sort(key=lambda item: len(item["title"]))
    return matches[0]


def focus_window_by_title(title: str, exact: bool = False) -> tuple[bool, str]:
    match = _find_window(title, exact=exact)
    if match is None:
        return False, f"No visible window matches '{title}'."
    hwnd = match["hwnd"]
    if not user32.IsWindow(hwnd):
        return False, "The window no longer exists."
    user32.ShowWindow(hwnd, SW_RESTORE)
    foreground = user32.GetForegroundWindow()
    target_tid = user32.GetWindowThreadProcessId(hwnd, None)
    foreground_tid = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    current_tid = kernel32.GetCurrentThreadId()
    attached: list[tuple[int, int]] = []
    try:
        for tid in {target_tid, foreground_tid}:
            if tid and tid != current_tid:
                if user32.AttachThreadInput(current_tid, tid, True):
                    attached.append((current_tid, tid))
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        for source, target in attached:
            user32.AttachThreadInput(source, target, False)
    time.sleep(0.15)
    ok = int(user32.GetForegroundWindow()) == int(hwnd) or match["title"].lower() in _window_title(user32.GetForegroundWindow()).lower()
    if not ok:
        return False, "Windows did not allow the assistant to bring that window to the foreground."
    return True, f"Focused '{match['title']}'."


# Launching ---------------------------------------------------------------
def _shortcuts() -> list[dict[str, str]]:
    global _cached_shortcuts
    if _cached_shortcuts and time.time() - _cached_shortcuts[0] < 300:
        return _cached_shortcuts[1]
    roots = [
        Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    ]
    found: list[dict[str, str]] = []
    for root in roots:
        if not root.exists():
            continue
        try:
            for path in root.rglob("*.lnk"):
                found.append({"name": path.stem, "path": str(path)})
                if len(found) > 5000:
                    break
        except OSError:
            continue
    found.sort(key=lambda item: item["name"].lower())
    _cached_shortcuts = (time.time(), found)
    return found


def _registry_app_paths() -> list[dict[str, str]]:
    import winreg

    results: list[dict[str, str]] = []
    keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
    ]
    for hive, subkey in keys:
        try:
            with winreg.OpenKey(hive, subkey) as root:
                count = winreg.QueryInfoKey(root)[0]
                for index in range(min(count, 1000)):
                    try:
                        name = winreg.EnumKey(root, index)
                        with winreg.OpenKey(root, name) as entry:
                            value, _ = winreg.QueryValueEx(entry, "")
                            if isinstance(value, str) and value:
                                results.append({"name": Path(name).stem, "path": value.strip('"')})
                    except OSError:
                        continue
        except OSError:
            continue
    return results


def _resolve_app(app: str) -> dict[str, Any]:
    query = app.strip().lower()
    for key, spec in KNOWN_APPS.items():
        if query == key or query in [alias.lower() for alias in spec.get("aliases", [])] or query == spec["title"].lower():
            if spec.get("uri"):
                return {"kind": "uri", "uri": spec["uri"], "name": spec["title"]}
            if spec.get("launch"):
                executable = shutil.which(spec["launch"])
                if executable:
                    return {"kind": "exe", "path": executable, "name": spec["title"]}
                system_exe = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / spec["launch"]
                if system_exe.exists():
                    return {"kind": "exe", "path": str(system_exe), "name": spec["title"]}
    shortcuts = _shortcuts()
    exact = [item for item in shortcuts if item["name"].lower() == query]
    partial = [item for item in shortcuts if query in item["name"].lower()]
    candidates = exact or partial
    if candidates:
        candidates.sort(key=lambda item: (len(item["name"]), item["name"].lower()))
        if not exact and len(candidates) > 3:
            return {
                "kind": "ambiguous",
                "name": query,
                "candidates": [item["name"] for item in candidates[:8]],
            }
        chosen = candidates[0]
        return {"kind": "shortcut", "path": chosen["path"], "name": chosen["name"]}
    for item in _registry_app_paths():
        if item["name"].lower() == query or query == Path(item["path"]).stem.lower():
            return {"kind": "exe", "path": item["path"], "name": item["name"]}
    executable = shutil.which(app)
    if executable:
        path = Path(executable)
        if path.suffix.lower() in {".exe", ".com", ".bat", ".cmd"}:
            return {"kind": "exe", "path": str(path), "name": path.stem}
    return {}


def _processes_by_hint(name: str) -> list[psutil.Process]:
    lowered = name.lower()
    matches = []
    for process in psutil.process_iter(["name", "exe"]):
        try:
            pname = (process.info.get("name") or "").lower()
            pexe = (process.info.get("exe") or "").lower()
            if pname == lowered or Path(pname).stem == Path(lowered).stem or Path(pexe).stem == Path(lowered).stem:
                matches.append(process)
        except (psutil.Error, OSError):
            continue
    return matches


def _launch(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    app = args["app"].strip()
    resolved = _resolve_app(app)
    if not resolved:
        return ToolResult.error(
            "apps.launch",
            f"'{app}' is not a known application or an installed Start Menu application. "
            "Use apps.list_installed to see candidates.",
        )
    if resolved.get("kind") == "ambiguous":
        return ToolResult.error(
            "apps.launch",
            f"'{app}' is ambiguous. Candidates: {human_join(resolved.get('candidates', []), limit=8)}. "
            "Please use a more specific name.",
        )
    before_windows = {window["hwnd"] for window in enumerate_windows()}
    before_pids = {process.pid for process in psutil.process_iter()}
    _check_cancel(ctx)
    try:
        if resolved["kind"] == "uri":
            os.startfile(resolved["uri"])  # type: ignore[attr-defined]
        elif resolved["kind"] == "shortcut":
            os.startfile(resolved["path"])  # type: ignore[attr-defined]
        else:
            executable = str(resolved["path"])
            if Path(executable).suffix.lower() in {".bat", ".cmd", ".ps1", ".vbs", ".js"}:
                return ToolResult.error("apps.launch", "Script files cannot be launched through this tool.")
            subprocess.Popen(
                [executable],
                cwd=str(Path(executable).parent),
                shell=False,
                close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
    except OSError as exc:
        return ToolResult.error("apps.launch", f"Launch failed: {exc.__class__.__name__}: {exc}")
    deadline = time.monotonic() + 8
    hint = resolved["name"].lower()
    observed: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        ctx.check_cancel()
        time.sleep(0.3)
        windows = enumerate_windows()
        new_windows = [window for window in windows if window["hwnd"] not in before_windows]
        if new_windows:
            def score(window: dict[str, Any]) -> tuple[int, int]:
                title = window["title"].lower()
                process = (window["process"] or "").lower()
                return (0 if hint in title or hint in process else 1, len(window["title"]))
            observed = sorted(new_windows, key=score)[0]
            break
        new_pids = {process.pid for process in psutil.process_iter()} - before_pids
        if new_pids:
            for pid in new_pids:
                try:
                    process = psutil.Process(pid)
                    if hint in (process.name() or "").lower() or hint in Path(process.exe() or "").stem.lower():
                        observed = {"pid": pid, "title": "", "process": process.name()}
                        break
                except (psutil.Error, OSError):
                    continue
            if observed:
                break
    if observed is None:
        return ToolResult.unverified(
            "apps.launch",
            f"Launch of '{resolved['name']}' was requested, but no new process or window was observed within 8 seconds. "
            "The application may have opened in an existing instance or may have failed.",
            data={"app": resolved["name"], "resolved": resolved},
        )
    return ToolResult.success(
        "apps.launch",
        f"Launched {resolved['name']} and observed process/window evidence.",
        data={"app": resolved["name"], "resolved": resolved, "observed": observed},
    )


def _list_installed(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    shortcuts = _shortcuts()
    registry_entries = _registry_app_paths()
    seen: set[str] = set()
    items: list[dict[str, str]] = []
    for item in shortcuts + registry_entries:
        key = item["name"].lower()
        if key and key not in seen:
            seen.add(key)
            items.append(item)
    items.sort(key=lambda item: item["name"].lower())
    return ToolResult.success(
        "apps.list_installed",
        f"Found {len(items)} Start Menu / registered application entries.",
        data={"applications": items[:1000], "total": len(items)},
    )


def _capabilities(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return ToolResult.success(
        "apps.capabilities",
        "Application capability registry loaded. Unknown applications support only launch/focus/close.",
        data={
            "known": KNOWN_APPS,
            "limitations": [
                "No full UI Automation tree is implemented in this milestone.",
                "Typing cannot be content-verified without an accessibility backend.",
                "Closing apps is sensitive and requires approval; force-kill is privileged.",
                "Applications may ignore WM_CLOSE or run elevated where automation is blocked.",
            ],
        },
    )


def _list_windows(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    windows = [window for window in enumerate_windows() if window["title"].strip()]
    windows.sort(key=lambda item: item["title"].lower())
    return ToolResult.success(
        "apps.list_windows",
        f"Found {len(windows)} visible top-level window(s).",
        data={"windows": windows[:500]},
    )


def _focus_window(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    ok, message = focus_window_by_title(args["title"], exact=bool(args.get("exact", False)))
    if not ok:
        return ToolResult.error("apps.focus_window", message)
    return ToolResult.success("apps.focus_window", message, data={"title": args["title"]})


def _type_risk(args: dict[str, Any]) -> int:
    title = str(args.get("window_title") or "").lower()
    if "notepad" in title or "wordpad" in title:
        return RiskLevel.REVERSIBLE
    windows = enumerate_windows()
    # GetForegroundWindow returns NULL when the desktop has no active window,
    # for example while the session is locked. With no verified safe target,
    # typing remains sensitive rather than failing or guessing.
    foreground = int(user32.GetForegroundWindow() or 0)
    for window in windows:
        if window["hwnd"] == foreground and (window["process"] or "").lower() in _SAFE_TYPE_PROCESSES:
            return RiskLevel.REVERSIBLE
    return RiskLevel.SENSITIVE


def _send_unicode_text(text: str, ctx: ToolContext) -> int:
    user32.SendInput.argtypes = (wintypes.UINT, ctypes.c_void_p, ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT
    ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class INPUTUNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", INPUTUNION)]

    KEYEVENTF_UNICODE = 0x0004
    KEYEVENTF_KEYUP = 0x0002
    INPUT_KEYBOARD = 1
    sent = 0

    def send_scan(scan: int, flags: int) -> bool:
        item = INPUT(type=INPUT_KEYBOARD)
        item.ki = KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
        return user32.SendInput(1, ctypes.cast(ctypes.byref(item), ctypes.c_void_p), ctypes.sizeof(INPUT)) == 1

    def send_vk(vk: int, flags: int) -> bool:
        item = INPUT(type=INPUT_KEYBOARD)
        item.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)
        return user32.SendInput(1, ctypes.cast(ctypes.byref(item), ctypes.c_void_p), ctypes.sizeof(INPUT)) == 1

    units: list[int] = []
    for char in text:
        if char == "\n":
            units.append(0x0D)
        elif char == "\t":
            units.append(0x09)
        else:
            encoded = char.encode("utf-16-le")
            for index in range(0, len(encoded), 2):
                units.append(int.from_bytes(encoded[index:index + 2], "little"))
    for unit in units:
        if ctx.cancelled:
            raise ValidationError("Typing was cancelled.")
        if unit in (0x0D, 0x09):
            vk = 0x0D if unit == 0x0D else 0x09
            if not send_vk(vk, 0) or not send_vk(vk, KEYEVENTF_KEYUP):
                raise OSError("SendInput failed for a control key.")
        else:
            if not send_scan(unit, KEYEVENTF_UNICODE) or not send_scan(unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
                raise OSError("SendInput failed for a Unicode character.")
        sent += 1
        time.sleep(0.001)
    return sent


def _type_text(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    text = args["text"]
    if not text.strip():
        return ToolResult.error("apps.type_text", "No text was provided.")
    window_title = args.get("window_title")
    if window_title:
        ok, message = focus_window_by_title(str(window_title))
        if not ok:
            return ToolResult.error("apps.type_text", message)
    _check_cancel(ctx)
    try:
        sent = _send_unicode_text(text, ctx)
    except (OSError, ValidationError) as exc:
        return ToolResult.error("apps.type_text", f"Typing failed: {exc}")
    focused = _window_title(user32.GetForegroundWindow())
    return ToolResult.unverified(
        "apps.type_text",
        f"Sent {sent} Unicode keystroke(s) to the foreground window. "
        "The assistant cannot read back the typed content, so success is unverified.",
        data={"characters": len(text), "keystrokes": sent, "foreground_title": focused, "verification": "not available"},
    )


def _close(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    name = args["name"].strip()
    matches = _processes_by_hint(name)
    if not matches:
        return ToolResult.error("apps.close", f"No running process matches '{name}'.")
    # Only operate when unambiguous unless the user explicitly named the exact process.
    exact = [process for process in matches if (process.name() or "").lower() == name.lower()]
    candidates = exact or matches
    unique: dict[int, psutil.Process] = {process.pid: process for process in candidates}
    if len(unique) != 1:
        return ToolResult.error(
            "apps.close",
            f"{len(unique)} processes match '{name}'. Narrow the name or use Task Manager. "
            "No process was closed.",
        )
    process = next(iter(unique.values()))
    pid = process.pid
    windows = [window for window in enumerate_windows() if window["pid"] == pid]
    for window in windows:
        user32.PostMessageW(window["hwnd"], WM_CLOSE, 0, 0)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        time.sleep(0.2)
        try:
            stopped = not process.is_running() or process.status() == psutil.STATUS_ZOMBIE
        except psutil.Error:
            stopped = not process.is_running()
        if stopped:
            return ToolResult.success(
                "apps.close",
                f"Closed {name} gracefully.",
                data={"pid": pid, "windows_closed": len(windows)},
            )
    if not args.get("force"):
        return ToolResult.error(
            "apps.close",
            f"{name} is still running after a graceful close request. Force-kill was not used. "
            "Save your work and retry with force approval if necessary.",
        )
    try:
        process.kill()
        process.wait(timeout=5)
    except (psutil.Error, OSError) as exc:
        return ToolResult.error("apps.close", f"Force-kill failed: {exc.__class__.__name__}")
    return ToolResult.success(
        "apps.close",
        f"Force-killed {name} after a graceful close did not work.",
        data={"pid": pid, "forced": True},
    )


def _check_cancel(ctx: ToolContext) -> None:
    ctx.check_cancel()
