"""Withdrawn Tkinter smoke test.

The window is created, immediately withdrawn, and driven without a main loop.
No microphone, speech, clipboard, or external application is used.
"""

from __future__ import annotations

import time

import pytest

tkinter = pytest.importorskip("tkinter")

from personal_assistant.core.permissions import ApprovalBridge  # noqa: E402
from personal_assistant.ui.app import AssistantWindow  # noqa: E402


@pytest.mark.ui
def test_window_constructs_routes_a_command_and_closes(make_assistant) -> None:
    assistant, _approver = make_assistant()
    bridge = ApprovalBridge(timeout_seconds=5)
    try:
        window = AssistantWindow(assistant, bridge, assistant.config)
    except tkinter.TclError as exc:
        pytest.skip(f"No interactive Windows desktop is available: {exc}")

    try:
        window.root.withdraw()
        window.root.update_idletasks()
        assert "Local assistant ready" in window.transcript.get("1.0", "end")
        assert window.badges["offline"].cget("text") == "offline: strict"
        assert window.badges["memory"].cget("text") == "memory on"

        window.submit("status")
        deadline = time.monotonic() + 10

        def check_finished() -> None:
            if not window.busy or time.monotonic() >= deadline:
                window.root.quit()
            else:
                window.root.after(25, check_finished)

        window.root.after(25, check_finished)
        window.root.after(11000, window.root.quit)
        window.root.mainloop()
        assert not window.busy, "the UI command did not complete"
        window.root.update()
        transcript = window.transcript.get("1.0", "end")
        assert "Offline mode: strict" in transcript
        assert "Kill switch: off" in transcript

        window.activate_kill_switch()
        assert assistant.safety.halted
        assert window.badges["safety"].cget("text") == "KILL SWITCH"

        window.reset_kill_switch()
        assert not assistant.safety.halted
        assert window.badges["safety"].cget("text") == "actions allowed"
    finally:
        if not window.closing:
            window._on_close()
        else:
            try:
                window.root.destroy()
            except tkinter.TclError:
                pass
