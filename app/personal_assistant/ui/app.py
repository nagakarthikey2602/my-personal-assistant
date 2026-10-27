"""Tkinter desktop interface for the local assistant.

The UI is intentionally thin: every command goes through ``Assistant`` and the
permissioned tool executor. The only direct reads are local memory inspection
and status display. Tkinter is used because this Python 3.14 environment has no
compatible PySide6 wheel; the UI therefore uses the Python standard library.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from ..config import Config
from ..core.assistant import Assistant, AssistantResponse
from ..core.permissions import ApprovalBridge, ApprovalRequest

logger = logging.getLogger(__name__)

APP_TITLE = "Local Personal Assistant"
BG = "#10151c"
PANEL = "#18202a"
PANEL_ALT = "#202b37"
TEXT = "#e6edf5"
MUTED = "#93a4b8"
ACCENT = "#5aa9e6"
USER_COLOR = "#8fd694"
ASSISTANT_COLOR = "#dce7f3"
ERROR_COLOR = "#ff8f8f"
SYSTEM_COLOR = "#c9b8ff"
WARN_COLOR = "#ffd27d"


class AssistantWindow:
    def __init__(self, assistant: Assistant, bridge: ApprovalBridge, config: Config) -> None:
        self.assistant = assistant
        self.bridge = bridge
        self.config = config
        self.busy = False
        self.recording = False
        self.closing = False
        self._speech_thread: threading.Thread | None = None

        self.root = tk.Tk()
        self.root.title(f"{APP_TITLE} — offline")
        self.root.geometry("1060x760")
        self.root.minsize(820, 560)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.report_callback_exception = self._tk_exception
        self._center()

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=TEXT, fieldbackground=PANEL_ALT)
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Badge.TLabel", background=PANEL_ALT, foreground=TEXT, padding=(8, 2), font=("Segoe UI", 9))
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=("Segoe UI Semibold", 16))
        style.configure("TButton", padding=(10, 5), font=("Segoe UI", 9))
        style.configure("Accent.TButton", padding=(12, 6), font=("Segoe UI Semibold", 10))
        style.configure("Danger.TButton", foreground="#ffb4b4")
        style.configure("TEntry", fieldbackground=PANEL_ALT, foreground=TEXT, insertcolor=TEXT)
        style.map("TEntry", fieldbackground=[("readonly", PANEL)])
        style.configure("TNotebook", background=BG)
        style.configure("TNotebook.Tab", padding=(12, 5))

        self.bridge.set_notifier(self._approval_notifier)
        self._build_menu()
        self._build_layout()
        self._refresh_badges()
        self._append(
            "Local assistant ready. All actions go through the permissioned tool registry.\n"
            f"{len(self.assistant.registry.names())} tools registered. "
            f"Offline mode: {self.config.get('app.offline_mode')}. "
            "Type a command or use Push to talk. Type 'help' for examples.\n",
            "system",
        )
        if self.config.get("app.ui.start_minimised"):
            self.root.after(200, self.root.iconify)

    # Window construction -------------------------------------------------
    def _center(self) -> None:
        self.root.update_idletasks()
        width = min(1060, max(820, self.root.winfo_screenwidth() - 160))
        height = min(760, max(560, self.root.winfo_screenheight() - 160))
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _build_menu(self) -> None:
        menu = tk.Menu(self.root, tearoff=False)
        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="Memory inspector…", command=self.open_memory)
        file_menu.add_command(label="Export memory…", command=self.export_memory_dialog)
        file_menu.add_separator()
        file_menu.add_command(label="Settings…", command=self.open_settings)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        menu.add_cascade(label="File", menu=file_menu)

        tools_menu = tk.Menu(menu, tearoff=False)
        tools_menu.add_command(label="Status", command=lambda: self.submit("status"))
        tools_menu.add_command(label="Capabilities", command=lambda: self.submit("capabilities"))
        tools_menu.add_command(label="Tool catalog…", command=self.open_catalog)
        tools_menu.add_separator()
        tools_menu.add_command(label="Memory inspector…", command=self.open_memory)
        tools_menu.add_command(label="Settings…", command=self.open_settings)
        menu.add_cascade(label="Tools", menu=tools_menu)

        safety_menu = tk.Menu(menu, tearoff=False)
        safety_menu.add_command(label="Stop current task", command=self.stop_current)
        safety_menu.add_command(label="Activate kill switch", command=self.activate_kill_switch)
        safety_menu.add_command(label="Reset kill switch", command=self.reset_kill_switch)
        menu.add_cascade(label="Safety", menu=safety_menu)

        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label="Help", command=lambda: self.submit("help"))
        help_menu.add_command(label="Limitations", command=self.show_limitations)
        help_menu.add_command(label="About", command=self.show_about)
        menu.add_cascade(label="Help", menu=help_menu)
        self.root.config(menu=menu)

    def _build_layout(self) -> None:
        header = ttk.Frame(self.root, padding=(14, 10, 14, 4))
        header.pack(fill="x")
        ttk.Label(header, text=APP_TITLE, style="Title.TLabel").pack(side="left")
        self.badge_frame = ttk.Frame(header)
        self.badge_frame.pack(side="right")
        self.badges: dict[str, ttk.Label] = {}
        for key in ("offline", "safety", "memory", "model"):
            label = ttk.Label(self.badge_frame, text="", style="Badge.TLabel")
            label.pack(side="left", padx=(5, 0))
            self.badges[key] = label

        transcript_frame = ttk.Frame(self.root, padding=(14, 4, 14, 4))
        transcript_frame.pack(fill="both", expand=True)
        self.transcript = tk.Text(
            transcript_frame,
            wrap="word",
            state="disabled",
            bg="#0b1016",
            fg=TEXT,
            insertbackground=TEXT,
            selectbackground="#2d4d68",
            relief="flat",
            padx=14,
            pady=12,
            font=("Segoe UI", 10),
            spacing1=2,
            spacing3=4,
        )
        scrollbar = ttk.Scrollbar(transcript_frame, orient="vertical", command=self.transcript.yview)
        self.transcript.configure(yscrollcommand=scrollbar.set)
        self.transcript.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.transcript.tag_configure("user", foreground=USER_COLOR, font=("Segoe UI Semibold", 10), spacing1=9)
        self.transcript.tag_configure("assistant", foreground=ASSISTANT_COLOR)
        self.transcript.tag_configure("error", foreground=ERROR_COLOR)
        self.transcript.tag_configure("system", foreground=SYSTEM_COLOR, font=("Segoe UI", 9))
        self.transcript.tag_configure("muted", foreground=MUTED, font=("Segoe UI", 9))

        input_frame = ttk.Frame(self.root, padding=(14, 4, 14, 4))
        input_frame.pack(fill="x")
        self.entry = ttk.Entry(input_frame, font=("Segoe UI", 11))
        self.entry.pack(side="left", fill="x", expand=True, ipady=5)
        self.entry.bind("<Return>", lambda _event: self.submit())
        self.entry.bind("<Escape>", lambda _event: self.stop_current())
        self.send_button = ttk.Button(input_frame, text="Send", style="Accent.TButton", command=self.submit)
        self.send_button.pack(side="left", padx=(8, 0))

        buttons = ttk.Frame(self.root, padding=(14, 2, 14, 6))
        buttons.pack(fill="x")
        self.voice_button = ttk.Button(buttons, text="🎙 Push to talk", command=self.push_to_talk)
        self.voice_button.pack(side="left")
        self.stop_button = ttk.Button(buttons, text="Stop", command=self.stop_current, state="disabled")
        self.stop_button.pack(side="left", padx=(6, 0))
        self.kill_button = ttk.Button(buttons, text="Kill switch", style="Danger.TButton", command=self.activate_kill_switch)
        self.kill_button.pack(side="left", padx=(6, 0))
        self.resume_button = ttk.Button(buttons, text="Resume", command=self.reset_kill_switch, state="disabled")
        self.resume_button.pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Status", command=lambda: self.submit("status")).pack(side="left", padx=(14, 0))
        ttk.Button(buttons, text="Help", command=lambda: self.submit("help")).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Capabilities", command=lambda: self.submit("capabilities")).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Memory", command=self.open_memory).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Settings", command=self.open_settings).pack(side="left", padx=(6, 0))

        status_frame = ttk.Frame(self.root, padding=(14, 2, 14, 8))
        status_frame.pack(fill="x")
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(status_frame, textvariable=self.status_var, style="Muted.TLabel").pack(side="left")
        self.activity_var = tk.StringVar(value="")
        ttk.Label(status_frame, textvariable=self.activity_var, style="Muted.TLabel").pack(side="right")

        self.entry.focus_set()
        self.root.bind("<Control-l>", lambda _event: self.clear_transcript())
        self.root.bind("<F1>", lambda _event: self.submit("help"))
        self.root.bind("<Control-q>", lambda _event: self._on_close())

    # Transcript ----------------------------------------------------------
    def _append(self, text: str, tag: str = "assistant") -> None:
        if self.closing:
            return
        self.transcript.configure(state="normal")
        if tag == "user":
            self.transcript.insert("end", "\nYou\n", "user")
        elif tag == "assistant":
            self.transcript.insert("end", "\nAssistant\n", "muted")
        elif tag == "error":
            self.transcript.insert("end", "\nAssistant\n", "muted")
        elif tag == "system":
            self.transcript.insert("end", "\n")
        self.transcript.insert("end", str(text).rstrip() + "\n", tag)
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def clear_transcript(self) -> None:
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.configure(state="disabled")

    # Command flow --------------------------------------------------------
    def submit(self, command: str | None = None) -> None:
        if self.closing:
            return
        if self.busy:
            self.status_var.set("An action is already running. Use Stop or wait for it to finish.")
            return
        text = (command if command is not None else self.entry.get()).strip()
        if not text:
            return
        if command is None:
            self.entry.delete(0, "end")
        self._append(text, "user")
        self._set_busy(True, "Working…")
        threading.Thread(target=self._run_text, args=(text,), daemon=True).start()

    def _run_text(self, text: str) -> None:
        try:
            response = self.assistant.handle_command(text, speak=False)
        except Exception as exc:
            logger.exception("UI command failed")
            self.root.after(0, lambda: self._complete_error(exc))
            return
        self.root.after(0, lambda: self._complete(response))

    def push_to_talk(self) -> None:
        if self.closing:
            return
        if self.busy:
            self.status_var.set("Wait for the current action to finish, or press Stop.")
            return
        self._append(
            "Recording push-to-talk audio. Speak one of your recorded phrases, then wait for the capture to end. "
            "Press Stop to end the recording early.",
            "system",
        )
        self.recording = True
        self._set_busy(True, "Listening…")
        self.voice_button.configure(text="🎙 Recording…")
        threading.Thread(target=self._run_voice, daemon=True).start()

    def _run_voice(self) -> None:
        try:
            response = self.assistant.listen_and_handle(speak=False)
        except Exception as exc:
            logger.exception("UI voice command failed")
            self.root.after(0, lambda: self._complete_error(exc))
            return
        self.root.after(0, lambda: self._complete(response))

    def _complete_error(self, exc: Exception) -> None:
        self._set_busy(False, "Ready.")
        self._append(f"An unexpected local error occurred: {exc.__class__.__name__}. Details are in the log.", "error")
        self._refresh_badges()

    def _complete(self, response: AssistantResponse) -> None:
        self.recording = False
        self._set_busy(False, "Ready.")
        tag = "error" if response.kind == "error" or not response.ok else "assistant"
        self._append(response.text or "(no response text)", tag)
        if response.details:
            self._append(" · ".join(response.details[:12]), "muted")
        self._refresh_badges()
        self._maybe_speak(response)
        if response.kind == "clarify":
            self.status_var.set("Nothing was run. Rephrase the command or type 'help'.")
        elif response.ok:
            state = response.outcome.state if response.outcome else "ok"
            self.status_var.set(f"Finished ({state}).")
        else:
            self.status_var.set("The command did not complete. See the transcript for details.")
        try:
            self.entry.focus_set()
        except tk.TclError:
            pass

    def _maybe_speak(self, response: AssistantResponse) -> None:
        if not self.config.get("app.ui.spoken_responses", True):
            return
        if not self.config.get("app.ui.voice_enabled", True):
            return
        if str(self.config.get("speech.tts_engine", "formant")) == "none":
            return
        text = (response.spoken_text or response.text or "").strip()
        if not text:
            return
        self.activity_var.set("Speaking locally…")

        def speak() -> None:
            ok, _message = self.assistant.speak(text)
            if not self.closing:
                self.root.after(0, lambda: self.activity_var.set("" if ok else "Speech output unavailable."))

        self._speech_thread = threading.Thread(target=speak, daemon=True)
        self._speech_thread.start()

    def _set_busy(self, busy: bool, message: str) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.entry.configure(state=state)
        self.send_button.configure(state=state)
        self.voice_button.configure(state=state)
        self.stop_button.configure(state="normal" if busy else "disabled")
        if not busy:
            self.voice_button.configure(text="🎙 Push to talk")
        self.status_var.set(message)

    # Safety --------------------------------------------------------------
    def stop_current(self) -> None:
        count = self.assistant.cancel()
        self.bridge.resolve(False)
        self.recording = False
        self._append(f"Stop requested. {count} active task token(s) cancelled; pending approval denied.", "system")
        self.status_var.set("Stopping…")
        self._refresh_badges()

    def activate_kill_switch(self) -> None:
        self.assistant.halt()
        self.bridge.resolve(False)
        self._append(
            "Kill switch activated. Running tasks were cancelled and new actions are blocked until Resume.",
            "system",
        )
        self.status_var.set("Kill switch active. No new action will run.")
        self._refresh_badges()

    def reset_kill_switch(self) -> None:
        self.assistant.resume()
        self._append("Kill switch reset. The normal permission policy applies again.", "system")
        self.status_var.set("Kill switch reset.")
        self._refresh_badges()

    def _refresh_badges(self) -> None:
        if self.closing:
            return
        halted = self.assistant.safety.halted
        memory_on = bool(self.config.get("memory.enabled", True)) and self.assistant.memory.enabled
        model_on = self.assistant.parser.model is not None
        values = {
            "offline": f"offline: {self.config.get('app.offline_mode')}",
            "safety": "KILL SWITCH" if halted else "actions allowed",
            "memory": "memory on" if memory_on else "memory off",
            "model": "intent model" if model_on else "rules only",
        }
        for key, text in values.items():
            label = self.badges.get(key)
            if label is not None:
                label.configure(text=text)
        self.kill_button.configure(state="disabled" if halted else "normal")
        self.resume_button.configure(state="normal" if halted else "disabled")
        voice_allowed = (
            bool(self.config.get("app.ui.voice_enabled", True))
            and str(self.config.get("speech.asr_engine", "template")) != "none"
        )
        if not self.busy:
            self.voice_button.configure(state="normal" if voice_allowed else "disabled")

    # Approval bridge -----------------------------------------------------
    def _approval_notifier(self, request: ApprovalRequest) -> None:
        self.root.after(0, lambda: self._show_approval(request))

    def _show_approval(self, request: ApprovalRequest) -> None:
        if self.closing:
            self.bridge.resolve(False)
            return
        window = tk.Toplevel(self.root)
        window.title("Approval required")
        window.transient(self.root)
        window.resizable(False, False)
        window.configure(bg=BG)
        window.protocol("WM_DELETE_WINDOW", lambda: decide(False))
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Approval required", style="Title.TLabel").pack(anchor="w")
        risk_color = ERROR_COLOR if request.risk >= 4 else WARN_COLOR
        ttk.Label(
            frame,
            text=f"{request.risk_label.upper()} — {request.tool}",
            foreground=risk_color,
            background=BG,
            font=("Segoe UI Semibold", 11),
        ).pack(anchor="w", pady=(8, 2))
        ttk.Label(frame, text=request.title, wraplength=540, justify="left").pack(anchor="w", pady=(0, 8))
        ttk.Label(
            frame,
            text=f"Why: {request.reason}",
            wraplength=540,
            justify="left",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(0, 8))
        preview = request.args_preview or "(no arguments)"
        box = tk.Text(
            frame,
            height=4,
            width=72,
            wrap="word",
            bg="#0b1016",
            fg=TEXT,
            relief="flat",
            padx=8,
            pady=6,
            font=("Consolas", 9),
        )
        box.insert("1.0", preview)
        box.configure(state="disabled")
        box.pack(fill="x", pady=(0, 8))
        note = (
            "This action is reversible through the undo journal where supported."
            if request.undoable
            else "This action is NOT covered by the filesystem undo journal."
        )
        ttk.Label(frame, text=note, wraplength=540, justify="left", style="Muted.TLabel").pack(anchor="w")
        ttk.Label(
            frame,
            text=f"If unanswered, it is denied automatically after {int(self.bridge.timeout)} seconds.",
            wraplength=540,
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(4, 10))

        decided = {"value": False}
        timer_id: list[str] = []

        def finish() -> None:
            try:
                if timer_id:
                    window.after_cancel(timer_id[0])
            except tk.TclError:
                pass
            try:
                window.grab_release()
                window.destroy()
            except tk.TclError:
                pass

        def decide(granted: bool) -> None:
            if decided["value"]:
                return
            decided["value"] = True
            self.bridge.resolve(granted)
            self._append(
                f"Approval {'granted' if granted else 'denied'} for {request.tool} ({request.risk_label}).",
                "system",
            )
            finish()

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(buttons, text="Deny (default)", command=lambda: decide(False)).pack(side="right")
        ttk.Button(buttons, text="Allow this action", style="Accent.TButton", command=lambda: decide(True)).pack(
            side="right", padx=(0, 8)
        )
        window.bind("<Escape>", lambda _event: decide(False))
        window.bind("<Return>", lambda _event: decide(True))
        window.update_idletasks()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - window.winfo_width()) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - window.winfo_height()) // 2)
        window.geometry(f"+{x}+{y}")
        window.grab_set()
        window.focus_set()

        def timed_out() -> None:
            if not decided["value"]:
                self._append(f"Approval timed out for {request.tool}; the action was denied.", "system")
                decide(False)

        timer_id.append(window.after(int(max(1.0, self.bridge.timeout) * 1000), timed_out))

    # Dialogs -------------------------------------------------------------
    def open_memory(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("Local memory")
        window.geometry("820x620")
        window.configure(bg=BG)
        frame = ttk.Frame(window, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Local memory inspector", style="Title.TLabel").pack(anchor="w")
        note = ttk.Label(
            frame,
            text=(
                "Data stays in the local SQLite database. Nothing is uploaded. "
                "Recent task commands and results may contain personal text."
            ),
            style="Muted.TLabel",
            wraplength=760,
            justify="left",
        )
        note.pack(anchor="w", pady=(4, 8))
        buttons = ttk.Frame(frame)
        buttons.pack(side="bottom", fill="x", pady=(8, 0))
        body = ttk.Frame(frame)
        body.pack(fill="both", expand=True)
        text = tk.Text(
            body,
            wrap="word",
            bg="#0b1016",
            fg=TEXT,
            relief="flat",
            padx=10,
            pady=8,
            font=("Consolas", 9),
        )
        scroll = ttk.Scrollbar(body, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        def refresh() -> None:
            try:
                status = self.assistant.memory.status()
                preferences = self.assistant.memory.list_preferences() if status.get("enabled") else []
                tasks = self.assistant.memory.recent_tasks(20, include_steps=False) if status.get("enabled") else []
            except Exception as exc:
                status = {"error": f"{exc.__class__.__name__}: {exc}"}
                preferences = []
                tasks = []
            text.configure(state="normal")
            text.delete("1.0", "end")
            text.insert("end", "STATUS\n" + json.dumps(status, indent=2, ensure_ascii=False) + "\n\n")
            text.insert("end", "PREFERENCES\n" + json.dumps(preferences, indent=2, ensure_ascii=False) + "\n\n")
            text.insert("end", "RECENT TASKS (20)\n" + json.dumps(tasks, indent=2, ensure_ascii=False))
            text.configure(state="disabled")
            enabled = bool(status.get("enabled"))
            disable_button.configure(text="Disable memory" if enabled else "Enable memory")
            export_button.configure(state="normal" if enabled else "disabled")
            delete_button.configure(state="normal" if enabled else "disabled")

        def export() -> None:
            initial = self.config.write_roots()[0] if self.config.write_roots() else self.config.root
            path = filedialog.asksaveasfilename(
                parent=window,
                title="Export local memory",
                initialdir=str(initial),
                initialfile=f"memory_export_{time.strftime('%Y%m%d_%H%M%S')}.json",
                defaultextension=".json",
                filetypes=[("JSON", "*.json"), ("All files", "*.*")],
            )
            if not path:
                return
            self._run_dialog_command(window, f'export memory to "{path}"', on_done=refresh)

        def delete_all() -> None:
            if not messagebox.askyesno(
                "Delete local memory?",
                "Delete all stored preferences and task history? Training examples are kept unless you say "
                "'forget everything including training'. This cannot be undone.",
                parent=window,
                default="no",
            ):
                return
            self._run_dialog_command(window, "forget everything", on_done=refresh)

        def toggle_memory() -> None:
            enabled = bool(self.assistant.memory.enabled)
            command = "disable memory" if enabled else "enable memory"
            if messagebox.askyesno(
                "Change memory setting?",
                f"{'Disable' if enabled else 'Enable'} local memory now?",
                parent=window,
                default="no",
            ):
                self._run_dialog_command(window, command, on_done=refresh)

        ttk.Button(buttons, text="Refresh", command=refresh).pack(side="left")
        export_button = ttk.Button(buttons, text="Export…", command=export)
        export_button.pack(side="left", padx=(6, 0))
        delete_button = ttk.Button(buttons, text="Delete all…", command=delete_all)
        delete_button.pack(side="left", padx=(6, 0))
        disable_button = ttk.Button(buttons, text="Disable memory", command=toggle_memory)
        disable_button.pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Close", command=window.destroy).pack(side="right")
        refresh()

    def export_memory_dialog(self) -> None:
        self.open_memory()

    def _run_dialog_command(self, window: tk.Toplevel, command: str, on_done: Callable[[], None] | None = None) -> None:
        def work() -> None:
            response = self.assistant.handle_command(command, speak=False)
            def done() -> None:
                if window.winfo_exists():
                    messagebox.showinfo(
                        "Assistant",
                        response.text,
                        parent=window if window.winfo_exists() else self.root,
                    )
                    if on_done:
                        on_done()
            self.root.after(0, done)

        threading.Thread(target=work, daemon=True).start()

    def open_settings(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("Assistant settings")
        window.geometry("620x480")
        window.configure(bg=BG)
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Settings", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            frame,
            text=(
                "Only safe UI and speech options are editable here. Paths, permissions, tool allowlists, "
                "and offline mode remain in config/config.yaml and are intentionally not weakened by this dialog."
            ),
            style="Muted.TLabel",
            wraplength=560,
            justify="left",
        ).pack(anchor="w", pady=(4, 12))

        spoken = tk.BooleanVar(value=bool(self.config.get("app.ui.spoken_responses", True)))
        voice = tk.BooleanVar(value=bool(self.config.get("app.ui.voice_enabled", True)))
        tts = tk.StringVar(value=str(self.config.get("speech.tts_engine", "formant")))
        asr = tk.StringVar(value=str(self.config.get("speech.asr_engine", "template")))
        memory = tk.BooleanVar(value=bool(self.config.get("memory.enabled", True)))

        ttk.Checkbutton(frame, text="Speak assistant responses (local TTS)", variable=spoken).pack(anchor="w")
        ttk.Checkbutton(frame, text="Enable voice input widgets", variable=voice).pack(anchor="w", pady=(4, 12))
        ttk.Checkbutton(frame, text="Enable local memory (preferences and task history)", variable=memory).pack(
            anchor="w", pady=(0, 12)
        )

        ttk.Label(frame, text="Speech output engine").pack(anchor="w")
        ttk.Radiobutton(frame, text="From-scratch formant synthesizer (no pretrained voice)", value="formant",
                        variable=tts).pack(anchor="w", padx=12)
        ttk.Radiobutton(frame, text="Disabled", value="none", variable=tts).pack(anchor="w", padx=12, pady=(0, 10))
        ttk.Label(
            frame,
            text="Microsoft SAPI voices are available only by editing config and explicitly acknowledging the model policy.",
            style="Muted.TLabel",
            wraplength=560,
            justify="left",
        ).pack(anchor="w", pady=(0, 10))

        ttk.Label(frame, text="Speech input engine").pack(anchor="w")
        ttk.Radiobutton(frame, text="User-recorded template matcher (closed vocabulary)", value="template",
                        variable=asr).pack(anchor="w", padx=12)
        ttk.Radiobutton(frame, text="Disabled", value="none", variable=asr).pack(anchor="w", padx=12, pady=(0, 10))

        source = self.config.source or (self.config.root / "config" / "config.yaml")
        ttk.Label(frame, text=f"Configuration file: {source}", style="Muted.TLabel", wraplength=560).pack(
            anchor="w", pady=(6, 12)
        )

        def save() -> None:
            try:
                new_tts = tts.get()
                if new_tts == "sapi" and not self.config.get("speech.allow_os_pretrained_tts"):
                    raise ValueError("SAPI requires explicit model-policy acknowledgement in the config file.")
                self.config.set("app.ui.spoken_responses", bool(spoken.get()))
                self.config.set("app.ui.voice_enabled", bool(voice.get()))
                self.config.set("memory.enabled", bool(memory.get()))
                self.config.set("speech.tts_engine", new_tts)
                self.config.set("speech.asr_engine", asr.get())
                if not memory.get():
                    self.assistant.memory.set_enabled(False)
                else:
                    self.assistant.memory.set_enabled(True)
                self.config.save()
                self.assistant.reload_speech()
                self._refresh_badges()
                self._append(f"Settings saved to {self.config.source}.", "system")
                window.destroy()
            except Exception as exc:
                messagebox.showerror("Could not save settings", f"{exc.__class__.__name__}: {exc}", parent=window)

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", side="bottom", pady=(16, 0))
        ttk.Button(buttons, text="Cancel", command=window.destroy).pack(side="right")
        ttk.Button(buttons, text="Save", style="Accent.TButton", command=save).pack(side="right", padx=(0, 8))

    def open_catalog(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("Tool catalog")
        window.geometry("900x560")
        window.configure(bg=BG)
        frame = ttk.Frame(window, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Registered tools", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            frame,
            text="Every action is registered here with a risk level and strict argument schema. Unlisted tools cannot run.",
            style="Muted.TLabel",
            wraplength=820,
        ).pack(anchor="w", pady=(2, 8))
        columns = ("name", "risk", "category", "description")
        tree = ttk.Treeview(frame, columns=columns, show="headings")
        for column, width in (("name", 190), ("risk", 130), ("category", 100), ("description", 420)):
            tree.heading(column, text=column.title())
            tree.column(column, width=width, anchor="w")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for spec in self.assistant.catalog():
            tree.insert(
                "",
                "end",
                values=(spec.get("name"), spec.get("risk_label"), spec.get("category"), spec.get("description")),
            )

    def show_limitations(self) -> None:
        messagebox.showinfo(
            "Honest limitations",
            "\n\n".join(f"• {item}" for item in self.assistant.LIMITATIONS),
            parent=self.root,
        )

    def show_about(self) -> None:
        messagebox.showinfo(
            "About",
            f"{APP_TITLE}\n\n"
            "Offline-first, local-only Windows assistant.\n"
            "No cloud AI, telemetry, or silent online fallback.\n\n"
            f"Python/Tkinter UI · {len(self.assistant.registry.names())} registered tools\n"
            f"Offline mode: {self.config.get('app.offline_mode')}\n"
            "Intent model: locally trained, label suggestion only.\n"
            "Speech: user-recorded template ASR and from-scratch formant TTS.",
            parent=self.root,
        )

    # Shutdown ------------------------------------------------------------
    def _tk_exception(self, exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        logger.error("Tkinter callback failed", exc_info=(exc_type, exc, tb))
        try:
            self._append(f"Interface error: {exc_type.__name__}: {exc}", "error")
        except Exception:
            pass

    def _on_close(self) -> None:
        if self.closing:
            return
        if self.busy:
            if not messagebox.askyesno(
                "Exit while busy?",
                "An action is still running. Cancel it and exit?",
                parent=self.root,
                default="no",
            ):
                return
        self.closing = True
        try:
            self.bridge.resolve(False)
        except Exception:
            pass
        try:
            self.assistant.shutdown()
        except Exception:
            logger.exception("Assistant shutdown failed")
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self) -> None:
        self.root.mainloop()
