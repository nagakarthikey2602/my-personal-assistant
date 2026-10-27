# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification for the local assistant.

Build from the repository root with scripts/package.ps1. The spec intentionally
does not collect personal data, memory databases, logs, sandbox contents, or
user-recorded voice templates. The locally trained intent model is loaded from
the application data directory at runtime (models/ in source, or the per-user
data directory for a frozen build) and is not embedded in the executable.
"""

from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
APP = ROOT / "app"
PKG = APP / "personal_assistant"

datas = [
    (str(PKG / "memory" / "schema.sql"), "personal_assistant/memory"),
    (str(PKG / "speech" / "sapi_tts.ps1"), "personal_assistant/speech"),
    (str(ROOT / "config" / "config.example.yaml"), "config"),
]

hiddenimports = [
    "yaml",
    "numpy",
    "psutil",
    "pypdf",
    "PIL.Image",
    "PIL.ImageGrab",
    "PIL.PngImagePlugin",
    "tkinter",
    "tkinter.ttk",
    "tkinter.filedialog",
    "tkinter.messagebox",
    "personal_assistant.main",
    "personal_assistant.ui.app",
    "personal_assistant.memory.store",
    "personal_assistant.intelligence.intent_model",
    "personal_assistant.speech.service",
    "personal_assistant.speech.template_asr",
    "personal_assistant.speech.tts",
    "personal_assistant.speech.native_audio",
    "personal_assistant.tools",
    "personal_assistant.tools.applications",
    "personal_assistant.tools.browser",
    "personal_assistant.tools.desktop",
    "personal_assistant.tools.dev",
    "personal_assistant.tools.documents",
    "personal_assistant.tools.filesystem",
    "personal_assistant.tools.memory_tools",
    "personal_assistant.tools.system",
    "personal_assistant.tools.voice_tools",
    "personal_assistant.tools.windows",
]

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(APP)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "_pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe_gui = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LocalPersonalAssistant",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

exe_cli = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LocalPersonalAssistantCLI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe_gui,
    exe_cli,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LocalPersonalAssistant",
)
