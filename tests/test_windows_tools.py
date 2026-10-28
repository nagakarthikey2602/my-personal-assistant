"""Safe Windows, application, and browser checks.

These tests only read status information or ask the permission policy to stop
before a launch. Real applications are never started, focused, typed into, or
closed here; that is reserved for the explicitly consented acceptance run.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

if os.name != "nt":
    pytest.skip("These checks exercise the Windows desktop session.", allow_module_level=True)

from personal_assistant.errors import PermissionDenied, ValidationError
from personal_assistant.tools import applications, windows

pytestmark = pytest.mark.windows


def test_windows_read_only_status_tools(tool, require_windows) -> None:
    call, _assistant = tool

    display = call("windows.display_info")
    assert display.status == "ok", display.message
    assert display.data["primary_width"] > 0
    assert display.data["primary_height"] > 0
    assert display.data["monitor_count"] >= 1
    assert display.data["dpi"] >= 48

    network = call("windows.network_status")
    assert network.status == "ok", network.message
    assert isinstance(network.data["interfaces"], list)
    assert "Network settings are not changed" in network.data["note"]

    bluetooth = call("windows.bluetooth_status")
    assert bluetooth.status == "ok", bluetooth.message
    assert isinstance(bluetooth.data["service_state"], str)

    power = call("windows.power_status")
    assert power.status == "ok", power.message
    assert isinstance(power.data["power_plan"], str)
    assert power.data["changing_power_settings"] == "not implemented"

    startup = call("windows.startup_list")
    assert startup.status == "ok", startup.message
    assert isinstance(startup.data["entries"], list)
    assert "does not modify" in startup.message


def test_open_settings_is_blocked_before_launch_without_approval(
    make_assistant, monkeypatch, workspace: Path
) -> None:
    assistant, approver = make_assistant(
        approve=False,
        overrides={"app": {"auto_approve_max_risk": 1}},
    )
    launched: list[str] = []
    monkeypatch.setattr(
        windows.os,
        "startfile",
        lambda target: launched.append(str(target)),
        raising=False,
    )
    token = assistant.safety.new_token("settings_policy")
    try:
        with pytest.raises(PermissionDenied):
            assistant.executor.execute(
                "windows.open_settings",
                {"page": "display"},
                token=token,
                task_id="settings_policy",
            )
    finally:
        assistant.safety.release_token(token)

    assert launched == []
    assert approver.requests
    assert approver.requests[-1].tool == "windows.open_settings"
    assert approver.requests[-1].risk == 2


def test_open_folder_outside_read_roots_is_denied_before_launch(
    tool, workspace: Path, monkeypatch
) -> None:
    call, _assistant = tool
    launched: list[list[str]] = []

    def fake_popen(args, **kwargs):  # pragma: no cover - must never run
        launched.append(list(args))
        raise AssertionError("File Explorer must not be launched for a denied path")

    monkeypatch.setattr(windows.subprocess, "Popen", fake_popen)
    outside = workspace / "config"
    outside.mkdir()
    result = call("windows.open_folder", path=str(outside))
    assert result.status == "error"
    assert launched == []
    assert "private internal data" in result.message or "denied" in result.message.lower()


def test_settings_schema_rejects_unknown_page(tool) -> None:
    call, _assistant = tool
    with pytest.raises(ValidationError):
        call("windows.open_settings", page="registry-editor")


def test_application_status_and_safe_misses(tool, monkeypatch) -> None:
    call, _assistant = tool

    capabilities = call("apps.capabilities")
    assert capabilities.status == "ok"
    assert "notepad" in capabilities.data["known"]
    assert any("UI Automation" in item for item in capabilities.data["limitations"])

    windows_list = call("apps.list_windows")
    assert windows_list.status == "ok"
    assert isinstance(windows_list.data["windows"], list)

    launched: list[object] = []
    monkeypatch.setattr(
        applications.subprocess,
        "Popen",
        lambda *args, **kwargs: launched.append(args),
    )
    missing = call("apps.launch", app="definitely-not-an-app-8f3c1d")
    assert missing.status == "error"
    assert launched == []

    focused = call("apps.focus_window", title="window-title-that-cannot-exist-8f3c1d", exact=True)
    assert focused.status == "error"

    closed = call("apps.close", name="process-that-cannot-exist-8f3c1d")
    assert closed.status == "error"
    assert "No running process" in closed.message


def test_type_text_risk_recognises_notepad(monkeypatch) -> None:
    from personal_assistant.tools import applications

    monkeypatch.setattr(applications, "enumerate_windows", lambda: [])
    monkeypatch.setattr(applications.user32, "GetForegroundWindow", lambda: None)
    assert applications._type_risk({"text": "hello", "window_title": "Untitled - Notepad"}) == 2
    assert applications._type_risk({"text": "hello", "window_title": "Bank portal"}) == 3


def test_browser_file_url_is_local_only(tool, workspace: Path, monkeypatch) -> None:
    call, _assistant = tool
    page = workspace / "sandbox" / "page.html"
    page.write_text("<html><body>local</body></html>", encoding="utf-8")
    opened: list[str] = []
    monkeypatch.setattr(
        "personal_assistant.tools.browser.os.startfile",
        lambda target: opened.append(str(target)),
        raising=False,
    )

    result = call("browser.open", url=page.as_uri())
    assert result.status == "unverified", result.message
    assert result.data["network_used"] is False
    assert len(opened) == 1
    assert opened[0].lower() == str(page).lower()


def test_browser_rejects_network_in_strict_mode(make_assistant) -> None:
    assistant, _approver = make_assistant(
        overrides={
            "app": {"offline_mode": "strict"},
            "tools": {"browser_network_enabled": True},
        }
    )
    token = assistant.safety.new_token("browser_strict")
    try:
        result = assistant.executor.execute(
            "browser.open",
            {"url": "https://example.invalid/"},
            token=token,
            task_id="browser_strict",
        )
    finally:
        assistant.safety.release_token(token)
    assert result.status == "error"
    assert "Strict offline mode" in result.message


def test_browser_network_requires_explicit_opt_in(make_assistant) -> None:
    assistant, _approver = make_assistant(
        overrides={
            "app": {"offline_mode": "advisory"},
            "tools": {"browser_network_enabled": False},
        }
    )
    token = assistant.safety.new_token("browser_opt_in")
    try:
        result = assistant.executor.execute(
            "browser.open",
            {"url": "https://example.invalid/"},
            token=token,
            task_id="browser_opt_in",
        )
    finally:
        assistant.safety.release_token(token)
    assert result.status == "error"
    assert "disabled in configuration" in result.message


def test_browser_scheme_allowlist_is_enforced(make_assistant) -> None:
    assistant, _approver = make_assistant(
        overrides={
            "app": {"offline_mode": "advisory"},
            "tools": {"browser_allowed_schemes": ["file"], "browser_network_enabled": True},
        }
    )
    token = assistant.safety.new_token("browser_scheme")
    try:
        result = assistant.executor.execute(
            "browser.open",
            {"url": "https://example.invalid/"},
            token=token,
            task_id="browser_scheme",
        )
    finally:
        assistant.safety.release_token(token)
    assert result.status == "error"
    assert "scheme" in result.message.lower()


def test_browser_capabilities_are_honest(tool) -> None:
    call, _assistant = tool
    result = call("browser.capabilities")
    assert result.status == "ok"
    assert "form filling" in result.data["not_implemented"]
    assert "untrusted data" in result.data["security"].lower()
