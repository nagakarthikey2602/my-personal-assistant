"""Tool modules. ``build_registry`` registers only real, implemented capabilities."""

from __future__ import annotations

from ..config import Config
from ..core.registry import ToolRegistry


def build_registry(config: Config) -> ToolRegistry:
    from . import applications, browser, desktop, dev, filesystem, memory_tools, system, voice_tools, windows

    registry = ToolRegistry()
    filesystem.register(registry)
    applications.register(registry)
    windows.register(registry)
    system.register(registry)
    desktop.register(registry)
    dev.register(registry)
    browser.register(registry)
    memory_tools.register(registry)
    voice_tools.register(registry)
    return registry
