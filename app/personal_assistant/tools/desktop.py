"""Clipboard and screenshot access through local Windows APIs."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import time
from pathlib import Path
from typing import Any

from ..core.context import ToolContext
from ..core.registry import RiskLevel, ToolSpec, object_schema
from ..core.results import ToolResult
from ..util import new_id, sha256_file

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
GMEM_ZEROINIT = 0x0040

user32.OpenClipboard.argtypes = (wintypes.HWND,)
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = (wintypes.UINT,)
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
user32.SetClipboardData.restype = wintypes.HANDLE
kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalFree.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalFree.restype = wintypes.HGLOBAL


def register(registry) -> None:  # type: ignore[no-untyped-def]
    registry.register(ToolSpec(
        name="desktop.clipboard_get",
        description="Read text from the Windows clipboard (local only, not logged).",
        risk=RiskLevel.READ_ONLY,
        category="desktop",
        schema=object_schema({}),
        handler=_clipboard_get,
    ))
    registry.register(ToolSpec(
        name="desktop.clipboard_set",
        description="Replace the Windows clipboard text.",
        risk=RiskLevel.REVERSIBLE,
        category="desktop",
        schema=object_schema({
            "text": {"type": "string", "minLength": 0, "maxLength": 100000},
        }, required=["text"]),
        handler=_clipboard_set,
    ))
    registry.register(ToolSpec(
        name="desktop.screenshot",
        description="Capture all screens to a PNG file inside an allowed write folder.",
        risk=RiskLevel.REVERSIBLE,
        category="desktop",
        undoable=True,
        dynamic_risk=lambda args: RiskLevel.SENSITIVE if args.get("overwrite") else RiskLevel.REVERSIBLE,
        schema=object_schema({
            "path": {"type": "string", "minLength": 1, "maxLength": 30000},
            "overwrite": {"type": "boolean", "default": False},
        }, required=["path"]),
        handler=_screenshot,
    ))


def _open_clipboard() -> None:
    for _ in range(10):
        if user32.OpenClipboard(None):
            return
        time.sleep(0.05)
    raise RuntimeError("Another application is holding the clipboard open.")


def _clipboard_get(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    if not ctx.config.get("tools.clipboard_enabled", True):
        return ToolResult.error("desktop.clipboard_get", "Clipboard access is disabled in configuration.")
    _open_clipboard()
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ToolResult.success("desktop.clipboard_get", "The clipboard contains no text.", data={"text": ""})
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ToolResult.error("desktop.clipboard_get", "Could not lock the clipboard memory.")
        try:
            text = ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()
    if len(text) > 100000:
        text = text[:100000]
    return ToolResult.success(
        "desktop.clipboard_get",
        f"Read {len(text)} characters from the clipboard.",
        data={"text": text, "truncated": len(text) >= 100000},
    )


def _clipboard_set(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    if not ctx.config.get("tools.clipboard_enabled", True):
        return ToolResult.error("desktop.clipboard_set", "Clipboard access is disabled in configuration.")
    text = args["text"]
    encoded = (text + "\x00").encode("utf-16-le")
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, len(encoded))
    if not handle:
        return ToolResult.error("desktop.clipboard_set", "Could not allocate clipboard memory.")
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        kernel32.GlobalFree(handle)
        return ToolResult.error("desktop.clipboard_set", "Could not lock clipboard memory.")
    try:
        ctypes.memmove(pointer, encoded, len(encoded))
    finally:
        kernel32.GlobalUnlock(handle)
    _open_clipboard()
    try:
        if not user32.EmptyClipboard():
            kernel32.GlobalFree(handle)
            return ToolResult.error("desktop.clipboard_set", "Could not clear the clipboard.")
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            return ToolResult.error("desktop.clipboard_set", "Windows rejected the clipboard update.")
        # Ownership transfers to Windows on success.
    finally:
        user32.CloseClipboard()
    return ToolResult.success(
        "desktop.clipboard_set",
        f"Copied {len(text)} characters to the clipboard.",
        data={"characters": len(text)},
    )


def _screenshot(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    if not ctx.config.get("tools.screenshot_enabled", True):
        return ToolResult.error("desktop.screenshot", "Screenshots are disabled in configuration.")
    path = ctx.path_policy.check_write(args["path"])
    if path.suffix.lower() != ".png":
        return ToolResult.error("desktop.screenshot", "The screenshot destination must end with .png.")
    if path.exists() and not bool(args.get("overwrite", False)):
        return ToolResult.error("desktop.screenshot", "The destination exists; overwrite requires explicit approval.")
    try:
        from PIL import ImageGrab
    except ImportError:
        return ToolResult.error("desktop.screenshot", "Pillow is not installed; screenshot support is unavailable.")
    ctx.check_cancel()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        image = ImageGrab.grab(all_screens=True)
        if image.width < 10 or image.height < 10:
            return ToolResult.error("desktop.screenshot", "The captured image was unexpectedly empty.")
        backup = None
        if path.exists():
            container = ctx.recovery.quarantine_root / f"backup_{new_id()}"
            container.mkdir(parents=True, exist_ok=False)
            backup = container / path.name
            path.replace(backup)
        image.save(path, format="PNG")
        if not path.exists() or path.stat().st_size < 100:
            return ToolResult.error("desktop.screenshot", "Screenshot file verification failed.")
        from PIL import Image

        with Image.open(path) as verify_image:
            verify_image.verify()
        actions = []
        if backup is not None:
            actions.append({"op": "restore_backup", "original_path": str(path), "backup_path": str(backup)})
        actions.append({"op": "quarantine_path", "path": str(path), "sha256": sha256_file(path)})
        undo_id = ctx.recovery.record(
            description=f"Screenshot {path.name}",
            tool="desktop.screenshot",
            task_id=ctx.task_id,
            actions=actions,
        )
        return ToolResult.success(
            "desktop.screenshot",
            f"Captured all screens to {path.name} and verified the PNG.",
            data={
                "path": str(path),
                "width": image.width,
                "height": image.height,
                "bytes": path.stat().st_size,
                "undo_id": undo_id,
                "privacy": "The image is stored locally and is not uploaded.",
            },
        )
    except (OSError, ValueError) as exc:
        return ToolResult.error("desktop.screenshot", f"Screenshot failed: {exc}")
