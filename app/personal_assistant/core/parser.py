"""Natural-language command parsing for the local assistant.

The parser is deliberately a closed, deterministic grammar first. A locally
trained softmax intent classifier is used only as a *label suggestion* when the
rules cannot understand a sentence. The classifier never supplies tool
arguments, never executes anything, and cannot bypass the registry, schema,
permission policy, or approval flow.

Every resulting action is a registered tool name plus explicit arguments. When
required details are missing the parser returns a clarification or an honest
"not understood" response rather than guessing.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from ..config import Config
from ..errors import ToolUnavailable, ValidationError

_CONFIDENCE_FLOOR = 0.50


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1].strip()
    return value


def _collapse(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _polite(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^(?:hey\s+)?(?:assistant|computer)[,:\s]+", "", text, flags=re.I)
    text = re.sub(
        r"^(?:(?:please|kindly)\s+)?(?:(?:could|would|can|will)\s+you\s+(?:please\s+)?|please\s+)+",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"^(?:please|kindly)\s+", "", text, flags=re.I)
    return text.strip()


def _norm(text: str) -> str:
    return _collapse(text.replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')).lower()


def _int(value: str | None, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _now_stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S", time.localtime())


def _split_quoted_list(text: str) -> list[str]:
    """Split on commas / semicolons / 'and' outside of quotes."""
    parts: list[str] = []
    current: list[str] = []
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "\"'":
            quote = char
            current.append(char)
            index += 1
            continue
        if char in ",;":
            parts.append("".join(current).strip())
            current = []
            index += 1
            continue
        if text[index:index + 5].lower() == " and ":
            parts.append("".join(current).strip())
            current = []
            index += 5
            continue
        current.append(char)
        index += 1
    parts.append("".join(current).strip())
    return [part for part in parts if part]


@dataclass
class PlannedStep:
    tool: str
    args: dict[str, Any]
    title: str = ""


@dataclass
class ParseResult:
    kind: str  # plan | builtin | clarify
    intent: str = ""
    steps: list[PlannedStep] = field(default_factory=list)
    message: str = ""
    source: str = "rules"
    confidence: float = 1.0
    alternatives: list[str] = field(default_factory=list)
    unknown: bool = False

    @property
    def is_action(self) -> bool:
        return self.kind == "plan" and bool(self.steps)


class CommandParser:
    """Rule-first command parser with a local, trainable label fallback."""

    BUILTINS = {
        "builtin.help",
        "builtin.capabilities",
        "builtin.status",
        "builtin.halt",
        "builtin.resume",
        "builtin.stop",
        "builtin.exit",
        "builtin.listen",
    }

    # Labels used by scripts/generate_training_data.py and the intent model.
    ACTION_INTENTS = (
        "fs.search", "fs.read", "fs.list", "fs.write", "fs.create_folder",
        "fs.copy", "fs.move", "fs.rename", "fs.quarantine_delete", "fs.zip",
        "fs.extract", "fs.duplicates", "fs.inventory", "fs.undo_last",
        "apps.launch", "apps.list_windows", "apps.focus_window", "apps.close",
        "apps.list_installed", "apps.type_text", "apps.capabilities",
        "windows.display_info", "windows.network_status", "windows.bluetooth_status",
        "windows.power_status", "windows.startup_list", "windows.open_settings",
        "windows.open_folder",
        "system.info", "system.top_processes", "system.media_control",
        "system.battery", "system.audio_devices", "system.offline_status",
        "desktop.clipboard_get", "desktop.clipboard_set", "desktop.screenshot",
        "dev.run", "dev.analyze_log", "dev.status",
        "browser.open",
        "memory.remember", "memory.list", "memory.forget", "memory.status",
        "memory.export", "memory.add_example", "memory.set_enabled",
        "voice.status", "voice.list_devices", "voice.speak", "voice.capabilities",
    )

    def __init__(
        self,
        config: Config,
        *,
        model_path: str | Path | None = None,
        enable_model: bool | None = None,
    ) -> None:
        self.config = config
        self.model = None
        self.model_error = ""
        enabled = (
            bool(config.get("intelligence.enable_model", True))
            if enable_model is None
            else bool(enable_model)
        )
        if enabled:
            path = Path(model_path or config.get("intelligence.intent_model_path"))
            self._load_model(path)
        self._settings_pages = {
            "home": "home", "display": "display", "screen": "display",
            "sound": "sound", "audio": "sound", "bluetooth": "bluetooth",
            "network": "network", "wifi": "wifi", "wi-fi": "wifi",
            "power": "power", "sleep": "power", "notifications": "notifications",
            "apps": "apps", "applications": "apps", "startup": "startup",
            "accessibility": "accessibility", "about": "about", "privacy": "privacy",
        }
        self._handlers: dict[str, Callable[[str], ParseResult]] = {
            "fs.search": self._h_search,
            "fs.read": self._h_read,
            "fs.list": self._h_list,
            "fs.write": self._h_write,
            "fs.create_folder": self._h_create_folder,
            "fs.copy": self._h_copy,
            "fs.move": self._h_move,
            "fs.rename": self._h_rename,
            "fs.quarantine_delete": self._h_delete,
            "fs.zip": self._h_zip,
            "fs.extract": self._h_extract,
            "fs.duplicates": self._h_duplicates,
            "fs.inventory": self._h_inventory,
            "fs.undo_last": self._h_undo,
            "apps.launch": self._h_launch,
            "apps.list_windows": self._h_list_windows,
            "apps.focus_window": self._h_focus,
            "apps.close": self._h_close,
            "apps.list_installed": self._h_installed,
            "apps.type_text": self._h_type,
            "apps.capabilities": self._h_app_capabilities,
            "windows.display_info": self._h_display,
            "windows.network_status": self._h_network,
            "windows.bluetooth_status": self._h_bluetooth,
            "windows.power_status": self._h_power,
            "windows.startup_list": self._h_startup,
            "windows.open_settings": self._h_settings,
            "windows.open_folder": self._h_open_folder,
            "system.info": self._h_system_info,
            "system.top_processes": self._h_processes,
            "system.media_control": self._h_media,
            "system.battery": self._h_battery,
            "system.audio_devices": self._h_audio,
            "system.offline_status": self._h_offline,
            "desktop.clipboard_get": self._h_clipboard_get,
            "desktop.clipboard_set": self._h_clipboard_set,
            "desktop.screenshot": self._h_screenshot,
            "dev.run": self._h_dev_run,
            "dev.analyze_log": self._h_log,
            "dev.status": self._h_dev_status,
            "browser.open": self._h_browser,
            "memory.remember": self._h_remember,
            "memory.list": self._h_memory_list,
            "memory.forget": self._h_forget,
            "memory.status": self._h_memory_status,
            "memory.export": self._h_memory_export,
            "memory.add_example": self._h_add_example,
            "memory.set_enabled": self._h_memory_switch,
            "voice.status": self._h_voice_status,
            "voice.list_devices": self._h_voice_devices,
            "voice.speak": self._h_speak,
            "voice.capabilities": self._h_voice_capabilities,
        }

    # Model -----------------------------------------------------------------
    def _load_model(self, path: Path) -> None:
        try:
            from ..intelligence.intent_model import IntentModel

            if not path.exists():
                self.model_error = "The local intent model has not been trained yet."
                return
            self.model = IntentModel.load(path)
            self.model_error = ""
        except (ToolUnavailable, ValidationError, OSError, ValueError) as exc:
            self.model = None
            self.model_error = str(exc)

    def model_status(self) -> dict[str, Any]:
        if self.model is None:
            return {
                "available": False,
                "reason": self.model_error or "No local intent model is loaded.",
                "fallback": "deterministic rules",
            }
        return {
            "available": True,
            "path": str(self.model.path or ""),
            "labels": len(self.model.labels),
            "features": len(self.model.vocabulary),
            "policy": "Locally trained; label suggestion only; no tool arguments or approvals are inferred.",
            "metadata": self.model.metadata,
        }

    # Helpers ---------------------------------------------------------------
    def _plan(
        self,
        intent: str,
        steps: list[PlannedStep],
        *,
        source: str = "rules",
        confidence: float = 1.0,
        note: str = "",
    ) -> ParseResult:
        return ParseResult(
            kind="plan", intent=intent, steps=steps, message=note,
            source=source, confidence=confidence,
        )

    def _clarify(self, intent: str, message: str) -> ParseResult:
        return ParseResult(kind="clarify", intent=intent, message=message)

    def _unknown(self, message: str = "I did not understand that command.") -> ParseResult:
        return ParseResult(kind="clarify", message=message, unknown=True)

    def _builtin(self, name: str, message: str = "") -> ParseResult:
        return ParseResult(kind="builtin", intent=name, message=message)

    def _step(self, tool: str, args: dict[str, Any], title: str) -> PlannedStep:
        return PlannedStep(tool=tool, args=args, title=title)

    def _write_root(self) -> Path:
        roots = self.config.write_roots()
        return roots[0] if roots else self.config.root / "sandbox"

    def _default_read_root(self) -> Path:
        documents = Path.home() / "Documents"
        if documents.exists() and str(documents.resolve()).lower() in {
            str(item.resolve()).lower() for item in self.config.read_roots()
        }:
            return documents
        roots = self.config.read_roots()
        return roots[0] if roots else self.config.root

    def _alias(self, token: str) -> Path | None:
        token = _strip_quotes(token).strip().lower().replace("/", "\\")
        token = re.sub(r"^(?:my|the)\s+", "", token)
        home = Path.home()
        mapping = {
            "sandbox": self._write_root(),
            "downloads": home / "Downloads",
            "documents": home / "Documents",
            "docs": home / "Documents",
            "desktop": home / "Desktop",
            "home": home,
            "project": self.config.root,
            "app": self.config.root,
            "workspace": self.config.root,
        }
        if token in mapping:
            return mapping[token]
        for name in ("downloads", "documents", "docs", "desktop", "home", "sandbox", "project"):
            prefix = name + "\\"
            if token.startswith(prefix):
                rest = _strip_quotes(token[len(prefix):])
                return mapping[name] / rest if rest else mapping[name]
        return None

    def _resolve(
        self,
        value: str,
        *,
        for_write: bool = False,
        must_exist: bool = False,
        default: Path | None = None,
    ) -> Path:
        text = _strip_quotes(value).strip()
        text = re.sub(r"^(?:the|my)\s+", "", text, flags=re.I)
        text = re.sub(r"\s+(?:file|folder|directory)$", "", text, flags=re.I)
        text = text.strip().strip("\"'")
        if not text:
            raise ValidationError("A path is required.")
        alias = self._alias(text)
        if alias is not None:
            return alias.resolve(strict=False)
        expanded = Path(os.path.expandvars(os.path.expanduser(text)))
        if expanded.is_absolute():
            return expanded.resolve(strict=False)
        candidates: list[Path] = []
        if for_write:
            candidates = [self._write_root() / expanded]
        else:
            candidates = [
                self._write_root() / expanded,
                self.config.root / expanded,
                Path.home() / "Documents" / expanded,
                Path.home() / "Desktop" / expanded,
                Path.home() / "Downloads" / expanded,
            ]
            if default is not None:
                candidates.insert(0, default / expanded)
        if must_exist:
            for candidate in candidates:
                if candidate.exists():
                    return candidate.resolve(strict=False)
        base = candidates[0] if candidates else self._write_root() / expanded
        return base.resolve(strict=False)

    def _target(self, raw: str) -> str:
        value = _strip_quotes(raw.strip())
        value = re.sub(r"^(?:the|my)\s+", "", value, flags=re.I)
        value = re.sub(r"\s+(?:app|application|window)$", "", value, flags=re.I)
        return value.strip().strip("\"'")

    def _path_or_clarify(self, raw: str, intent: str, **kwargs: Any) -> Path | ParseResult:
        try:
            return self._resolve(raw, **kwargs)
        except ValidationError as exc:
            return self._clarify(intent, str(exc))

    def _location(self, text: str) -> tuple[str, str | None]:
        match = re.match(r"^(?P<head>.+?)\s+(?:in|inside|under|within)\s+(?P<loc>.+)$", text, re.I)
        if match:
            return match.group("head").strip(), match.group("loc").strip()
        return text.strip(), None

    def _file_candidate(self, text: str) -> str | None:
        quoted = re.search(r"\"([^\"]+)\"|'([^']+)'", text)
        if quoted:
            return quoted.group(1) or quoted.group(2)
        match = re.search(
            r"(?:[A-Za-z]:\\[^,;]+|(?:[\w .-]+\\)*[\w .-]+\.[A-Za-z0-9]{1,8})",
            text,
        )
        if match:
            return match.group(0).strip()
        return None

    # Public parse ----------------------------------------------------------
    def parse(self, text: str) -> ParseResult:
        command = _collapse(text.strip())
        if not command:
            return self._unknown("Please type or say a command.")
        command = _polite(command)
        if not command:
            return self._unknown("Please type or say a command.")

        builtin = self._builtin_rule(command)
        if builtin is not None:
            return builtin

        parts = [part.strip() for part in re.split(r"\s+(?:and\s+)?then\s+|;\s*", command, flags=re.I) if part.strip()]
        if len(parts) > 1:
            combined: list[PlannedStep] = []
            for part in parts:
                result = self._parse_single(part)
                if result.kind == "clarify":
                    return self._clarify(
                        "composite",
                        f"I could not plan the step \"{part}\": {result.message}",
                    )
                if result.kind != "plan":
                    return self._clarify(
                        "composite",
                        "I can chain file, app, and system actions, but each step must be an action. "
                        "Please issue control commands separately.",
                    )
                combined.extend(result.steps)
            return self._plan("composite", combined, note=f"Planned {len(combined)} steps.")

        result = self._parse_single(command)
        if result.kind == "plan" or not result.unknown:
            return result
        return self._model_fallback(command, result)

    def _model_fallback(self, command: str, base: ParseResult) -> ParseResult:
        if self.model is None:
            return base
        try:
            prediction = self.model.predict(command)
        except (ValidationError, ValueError):
            return base
        alternatives = [item["label"] for item in prediction.alternatives[1:4]]
        min_conf = float(self.config.get("intelligence.min_confidence", 0.55))
        min_margin = float(self.config.get("intelligence.min_margin", 0.15))
        if prediction.confidence < max(min_conf, _CONFIDENCE_FLOOR) or prediction.margin < min_margin:
            return ParseResult(
                kind="clarify",
                intent="model.low_confidence",
                message=(
                    "I did not understand that command. The local intent model suggested "
                    f"'{prediction.label}' with low confidence ({prediction.confidence:.0%}), "
                    "so I did not run anything. Please rephrase or use 'help'."
                ),
                source="model",
                confidence=prediction.confidence,
                alternatives=alternatives,
            )
        handler = self._handlers.get(prediction.label)
        if handler is None:
            return ParseResult(
                kind="clarify",
                intent=prediction.label,
                message=(
                    f"The local model suggested '{prediction.label}', but that intent is not "
                    "implemented as a safe action. Nothing was run."
                ),
                source="model",
                confidence=prediction.confidence,
            )
        parsed = handler(command)
        if parsed.kind == "plan" and parsed.steps:
            parsed.source = "model"
            parsed.confidence = prediction.confidence
            parsed.alternatives = alternatives
            note = (
                f"Intent label suggested by the local model ({prediction.confidence:.0%}); "
                "arguments came from deterministic text rules."
            )
            parsed.message = (parsed.message + " " + note).strip()
            return parsed
        if parsed.kind == "clarify":
            return ParseResult(
                kind="clarify",
                intent=prediction.label,
                message=(
                    f"The local model suggested '{prediction.label}', but the command is missing "
                    f"required details or is ambiguous. {parsed.message} Nothing was run."
                ),
                source="model",
                confidence=prediction.confidence,
                alternatives=alternatives,
            )
        return parsed

    # Rule selection --------------------------------------------------------
    def _parse_single(self, command: str) -> ParseResult:
        low = _norm(command).rstrip(".!?")

        # Stop/control words are exact and handled by the assistant, not tools.
        if low in {"stop", "cancel", "never mind", "nevermind"}:
            return self._builtin("builtin.stop")
        if low in {"kill switch", "activate kill switch", "halt", "emergency stop", "stop everything"}:
            return self._builtin("builtin.halt")
        if low in {"resume", "reset kill switch", "deactivate kill switch", "clear kill switch"}:
            return self._builtin("builtin.resume")
        if low in {"exit", "quit", "close the assistant", "shut down the assistant"}:
            return self._builtin("builtin.exit")
        if low in {"listen", "voice input", "record a command", "start listening"}:
            return self._builtin("builtin.listen")

        # Memory and voice have distinctive phrases; test them early.
        for label, pattern in (
            ("memory.add_example", r"^(?:add|remember)\s+(?:a\s+)?training example\b"),
            ("memory.remember", r"^remember\b"),
            ("memory.forget", r"^(?:forget|delete (?:all )?memory|erase (?:all )?memory)\b"),
            ("memory.export", r"^(?:export|save)\s+(?:my\s+)?memory\b"),
            ("memory.status", r"^(?:memory|assistant memory)\s+status\b"),
            (
                "memory.list",
                r"^(?:(?:list|show)\s+(?:my\s+)?(?:preferences|memory|memories)\b|"
                r"what (?:do you|did you) remember\b|memory list\b|preferences\b)",
            ),
            ("memory.set_enabled", r"^(?:disable|enable|turn (?:off|on))\s+(?:the\s+)?memory\b"),
        ):
            if re.search(pattern, low):
                return self._handlers[label](command)

        if re.search(r"^(?:voice|speech)\s+(?:status|devices|capabilities)\b", low):
            if "capabilit" in low:
                return self._h_voice_capabilities(command)
            if "device" in low:
                return self._h_voice_devices(command)
            return self._h_voice_status(command)
        if re.search(r"^(?:list|show)\s+(?:microphones|speakers|audio devices|sound devices)\b", low):
            return self._h_voice_devices(command)
        if re.search(r"^(?:speak|say)\b", low):
            return self._h_speak(command)

        if re.search(r"^(?:undo|revert)(?:\s+(?:the\s+)?(?:last|previous))?(?:\s+(?:action|operation|change))?$", low):
            return self._h_undo(command)

        if re.search(r"^(?:take\s+)?(?:a\s+)?screenshot\b", low) or re.search(r"^screenshot\b", low):
            return self._h_screenshot(command)

        if re.search(r"^(?:copy|put|write)\s+.+\s+(?:to|on|onto)\s+(?:the\s+)?clipboard$", low):
            return self._h_clipboard_set(command)
        if re.search(r"^(?:read|show|what(?:'s| is) (?:on )?)\s+(?:the\s+)?clipboard\b", low):
            return self._h_clipboard_get(command)
        if re.search(r"^set\s+(?:the\s+)?clipboard\s+(?:to|content)\b", low):
            return self._h_clipboard_set(command)

        # Developer commands before generic "run" app launching.
        if re.search(r"^(?:run|start|execute)\s+(?:the\s+)?(?:tests?|test suite|pytest|unit tests?)\b", low):
            return self._h_dev_run(command)
        if re.search(r"^(?:git\s+(?:status|log|diff)|compileall)\b", low):
            return self._h_dev_run(command)
        if re.search(r"^(?:run|execute)\s+(?:the\s+)?(?:git\s+)?(?:status|git log|git diff|compileall|compile)\b", low):
            return self._h_dev_run(command)
        if re.search(r"^(?:analy[sz]e|check|inspect)\s+(?:the\s+)?(?:build\s+|error\s+|test\s+)?log\b", low):
            return self._h_log(command)
        if re.search(r"^(?:dev|developer)\s+status\b", low):
            return self._h_dev_status(command)

        if re.search(r"^(?:open|go to|visit)\s+(?:https?|file)://", low):
            return self._h_browser(command)

        # Settings must precede the general open/launch rule.
        if re.search(r"^(?:open|show|launch)\s+(?:the\s+)?(?:windows\s+)?settings\b", low):
            return self._h_settings(command)
        if re.search(r"^(?:open|show)\s+(?:the\s+)?[a-z -]+\s+settings\b", low):
            return self._h_settings(command)

        # Read a document before generic open/launch.
        if re.search(r"^(?:read|open|show|display)\s+(?:the\s+)?(?:file|document)\b", low):
            return self._h_read(command)
        if re.search(r"^(?:read|show|display|open)\s+.+\.(?:txt|md|json|csv|log|pdf|docx|py|yaml|yml|toml|ini|html?)$", low):
            return self._h_read(command)

        # File and folder operations.
        if re.search(r"^(?:search|find|look for)\b", low):
            if re.search(r"\b(?:duplicate|duplicates|same)\b", low):
                return self._h_duplicates(command)
            if re.search(r"\binventory\b", low):
                return self._h_inventory(command)
            return self._h_search(command)
        if re.search(r"^(?:create|make)\s+(?:a\s+|an\s+)?(?:new\s+)?(?:folder|directory)\b", low):
            return self._h_create_folder(command)
        if re.search(r"^(?:create|make|write|save)\s+(?:a\s+|an\s+)?(?:new\s+)?(?:text\s+|word\s+|docx\s+)?file\b", low):
            return self._h_write(command)
        if re.search(r"^(?:write|put|save)\s+.+\s+(?:to|into)\s+.+", low):
            return self._h_write(command)
        if re.search(r"^(?:copy|duplicate)\s+.+\s+(?:to|into)\s+.+", low):
            return self._h_copy(command)
        if re.search(r"^(?:move|transfer)\s+.+\s+(?:to|into)\s+.+", low):
            return self._h_move(command)
        if re.search(r"^rename\s+.+\s+(?:to|as)\s+.+", low):
            return self._h_rename(command)
        if re.search(r"^(?:delete|remove|trash|quarantine)\s+(?:the\s+)?(?:file\s+|folder\s+|item\s+)?", low):
            return self._h_delete(command)
        if re.search(r"^(?:zip|compress|archive)\s+.+", low):
            return self._h_zip(command)
        if re.search(r"^(?:unzip|extract)\s+.+", low):
            return self._h_extract(command)
        if re.search(r"^(?:find\s+)?duplicates?\b", low):
            return self._h_duplicates(command)
        if re.search(r"\binventory\b", low):
            return self._h_inventory(command)
        if re.search(r"^(?:list|show)\s+(?:the\s+)?(?:files|contents|items)\b", low):
            return self._h_list(command)
        if re.search(r"^list\s+(?:downloads|documents|docs|desktop|sandbox|project|home)\b", low):
            return self._h_list(command)
        if re.search(r"^(?:list|show)\s+(?:the\s+)?(?:folder|directory)\b", low):
            return self._h_list(command)

        # Application/window operations.
        if re.search(r"^(?:list|show|what)\b.*\b(?:open\s+)?windows\b", low):
            return self._h_list_windows(command)
        if re.search(r"^(?:list|show)\s+(?:installed\s+)?(?:apps|applications)\b", low):
            return self._h_installed(command)
        if re.search(r"^(?:what|which)\s+(?:apps|applications)\s+are\s+installed\b", low):
            return self._h_installed(command)
        if re.search(r"^(?:app|application)\s+capabilities\b", low):
            return self._h_app_capabilities(command)
        if re.search(r"^(?:close|terminate|force close|force quit)\s+", low):
            return self._h_close(command)
        if re.search(r"^(?:switch to|focus|bring)\s+", low):
            return self._h_focus(command)
        if re.search(r"^type\s+", low):
            return self._h_type(command)
        if re.search(r"^(?:open|show)\s+(?:the\s+)?(?:folder|directory)\s+", low):
            return self._h_open_folder(command)
        if re.search(r"^(?:open|show)\s+\S+\s+(?:folder|directory)$", low):
            return self._h_open_folder(command)
        if re.search(r"^(?:open|launch|start|run)\s+", low):
            return self._h_launch(command)

        # Windows/system status.
        if re.search(r"^(?:system|computer|pc)\s+(?:info|information|specs|report)\b", low):
            return self._h_system_info(command)
        if re.search(r"\b(?:top|highest|most)\b.*\bprocesses?\b", low) or re.search(r"\b(?:cpu|memory)\s+usage\b", low) or "using memory" in low or "using cpu" in low:
            return self._h_processes(command)
        if re.search(r"^(?:media|playback)\s+control\b", low) or low in {"play", "pause", "play pause", "mute", "unmute", "volume up", "volume down", "next track", "previous track", "next song", "previous song"}:
            return self._h_media(command)
        if re.search(r"^battery\b", low) or re.search(r"\b(?:battery|charge)\s+(?:status|level|percent)\b", low):
            return self._h_battery(command)
        if re.search(r"^(?:display|screen|monitor)\s+(?:info|information|status|resolution)\b", low) or "screen resolution" in low:
            return self._h_display(command)
        if re.search(r"^(?:network|wifi|wi-fi|internet)\s+status\b", low):
            return self._h_network(command)
        if re.search(r"^bluetooth\s+(?:status|state)\b", low):
            return self._h_bluetooth(command)
        if re.search(r"^(?:power|sleep)\s+(?:status|plan|settings)\b", low) or "power plan" in low:
            return self._h_power(command)
        if re.search(r"^startup\s+(?:apps|applications|entries|list)\b", low):
            return self._h_startup(command)
        if re.search(r"^(?:audio|sound)\s+(?:devices|status)\b", low):
            return self._h_audio(command)
        if re.search(r"^(?:offline|network guard)\s+status\b", low) or low in {"are you offline", "offline mode"}:
            return self._h_offline(command)

        # Builtin informational commands last so "memory status" wins above.
        if low in {"help", "commands", "show commands", "what can i say"} or "what can you do" in low:
            return self._builtin("builtin.help")
        if low in {"capabilities", "your capabilities", "what are your capabilities", "limitations"}:
            return self._builtin("builtin.capabilities")
        if low in {"status", "assistant status", "are you ready", "health"}:
            return self._builtin("builtin.status")

        return self._unknown(
            "I did not understand that command. Nothing was run. "
            "Try 'help' to see supported commands, or rephrase using a specific action."
        )

    def _builtin_rule(self, command: str) -> ParseResult | None:
        low = _norm(command).rstrip(".!?")
        if low in {"help", "commands", "show commands", "what can i say"} or "what can you do" in low:
            return self._builtin("builtin.help")
        if low in {"capabilities", "your capabilities", "what are your capabilities", "limitations"}:
            return self._builtin("builtin.capabilities")
        if low in {"status", "assistant status", "are you ready", "health"}:
            return self._builtin("builtin.status")
        if low in {"stop", "cancel", "never mind", "nevermind"}:
            return self._builtin("builtin.stop")
        if low in {"kill switch", "activate kill switch", "halt", "emergency stop", "stop everything"}:
            return self._builtin("builtin.halt")
        if low in {"resume", "reset kill switch", "deactivate kill switch", "clear kill switch"}:
            return self._builtin("builtin.resume")
        if low in {"exit", "quit", "close the assistant", "shut down the assistant"}:
            return self._builtin("builtin.exit")
        return None

    # Handlers: memory ------------------------------------------------------
    def _h_remember(self, text: str) -> ParseResult:
        raw = re.sub(r"^remember\s+", "", _polite(text), flags=re.I).strip()
        raw = re.sub(r"^that\s+", "", raw, flags=re.I)
        match = re.match(r"^(?P<key>.+?)\s+(?:is|equals|should be)\s+(?P<value>.+)$", raw, re.I)
        if not match:
            match = re.match(r"^(?P<key>[^:=]{1,100}?)\s*[:=]\s*(?P<value>.+)$", raw)
        if match:
            key = re.sub(r"^(?:my|the)\s+", "", match.group("key").strip(), flags=re.I)
            key = _strip_quotes(key)
            value = _strip_quotes(match.group("value").strip())
            if key and value:
                return self._plan(
                    "memory.remember",
                    [self._step("memory.remember", {"key": key, "value": value}, f"Remember {key}")],
                )
        if not raw:
            return self._clarify("memory.remember", "What should I remember?")
        key = f"note_{_now_stamp()}"
        return self._plan(
            "memory.remember",
            [self._step("memory.remember", {"key": key, "value": raw[:5000]}, "Remember a note")],
        )

    def _h_memory_list(self, text: str) -> ParseResult:
        return self._plan(
            "memory.list",
            [self._step("memory.list", {"history_limit": 5}, "List local memory")],
        )

    def _h_forget(self, text: str) -> ParseResult:
        low = _norm(text)
        include_training = "training" in low
        if re.search(r"\b(?:all|everything|entire)\b", low) or low in {"forget memory", "erase memory", "delete memory"}:
            return self._plan(
                "memory.forget",
                [self._step("memory.forget", {"all": True, "include_training": include_training}, "Delete all local memory")],
            )
        match = re.search(r"(?:key|preference)\s+(?:called\s+)?(.+)$", text, re.I)
        key = _strip_quotes(match.group(1)) if match else _strip_quotes(re.sub(r"^(?:forget|delete|erase)\s+", "", _polite(text), flags=re.I))
        key = re.sub(r"^(?:the|my)\s+", "", key, flags=re.I).strip()
        if not key or key.lower() in {"memory", "everything", "all"}:
            return self._clarify("memory.forget", "Which preference should I delete? Say 'forget everything' to delete all local memory.")
        return self._plan(
            "memory.forget",
            [self._step("memory.forget", {"key": key, "all": False}, f"Forget '{key}'")],
        )

    def _h_memory_status(self, text: str) -> ParseResult:
        return self._plan("memory.status", [self._step("memory.status", {}, "Memory status")])

    def _h_memory_export(self, text: str) -> ParseResult:
        match = re.search(r"\b(?:to|into|at)\s+(.+)$", text, re.I)
        if match:
            path = self._path_or_clarify(match.group(1), "memory.export", for_write=True)
        else:
            path = self._write_root() / f"memory_export_{_now_stamp()}.json"
        if isinstance(path, ParseResult):
            return path
        return self._plan(
            "memory.export",
            [self._step("memory.export", {"path": str(path)}, f"Export memory to {path.name}")],
        )

    def _h_add_example(self, text: str) -> ParseResult:
        match = re.search(
            r"(?:add|remember)\s+(?:a\s+)?training example\s+(?:text\s+)?[\"']?(.+?)[\"']?\s+(?:as|for|label)\s+([a-z][a-z0-9_.]+)\s*$",
            text,
            re.I,
        )
        if not match:
            return self._clarify(
                "memory.add_example",
                "Use: add training example \"open notepad\" as apps.launch",
            )
        return self._plan(
            "memory.add_example",
            [self._step(
                "memory.add_example",
                {"text": match.group(1).strip(), "label": match.group(2).strip(), "approved": True},
                "Add an approved training example",
            )],
        )

    def _h_memory_switch(self, text: str) -> ParseResult:
        low = _norm(text)
        enabled = bool(re.search(r"\b(?:enable|turn on|on)\b", low)) and not re.search(r"\b(?:disable|turn off|off)\b", low)
        return self._plan(
            "memory.set_enabled",
            [self._step("memory.set_enabled", {"enabled": enabled}, "Enable local memory" if enabled else "Disable local memory")],
        )

    # Handlers: files -------------------------------------------------------
    def _h_search(self, text: str) -> ParseResult:
        raw = re.sub(r"^(?:search|find|look for)\s+", "", _polite(text), flags=re.I).strip()
        location: str | None = None
        query = raw
        # "search downloads for budget"
        match = re.match(r"^(?P<loc>\"[^\"]+\"|'[^']+'|\S+)\s+for\s+(?P<query>.+)$", raw, re.I)
        if match:
            location = match.group("loc")
            query = match.group("query")
        else:
            query, location = self._location(raw)
        query = re.sub(r"^(?:for|files?\s+(?:named|called)\s+|files?\s+containing\s+|files?\s+with\s+(?:text\s+)?)", "", query, flags=re.I).strip()
        query = _strip_quotes(query)
        content = ""
        content_match = re.search(r"\b(?:containing|with text|that contain|whose content includes)\s+(.+)$", query, re.I)
        if content_match:
            content = _strip_quotes(content_match.group(1)).strip()
            query = query[:content_match.start()].strip()
        extensions: list[str] = []
        ext_match = re.search(r"\b([a-z0-9]{1,10})\s+files?\b", query, re.I)
        if ext_match and ext_match.group(1).lower() in {
            "pdf", "docx", "txt", "md", "json", "csv", "log", "py", "yaml", "yml",
            "toml", "ini", "html", "htm", "xml", "jpg", "jpeg", "png", "zip",
        }:
            extensions = ["." + ext_match.group(1).lower()]
            query = re.sub(r"\b" + re.escape(ext_match.group(1)) + r"\s+files?\b", "", query, flags=re.I).strip()
        if query.lower() in {"", "files", "file", "anything"}:
            pattern = "*"
        elif "*" in query or "?" in query:
            pattern = query
        else:
            pattern = f"*{query}*"
        if location:
            root = self._path_or_clarify(location, "fs.search", must_exist=True, default=self._default_read_root())
        else:
            root = self._default_read_root()
        if isinstance(root, ParseResult):
            return root
        args: dict[str, Any] = {"path": str(root), "pattern": pattern, "max_results": 100}
        if content:
            args["content"] = content
        if extensions:
            args["extensions"] = extensions
        description = f"Search {root.name or str(root)} for {query or '*'}".strip()
        return self._plan("fs.search", [self._step("fs.search", args, description)])

    def _h_list(self, text: str) -> ParseResult:
        raw = re.sub(r"^(?:list|show)\s+(?:the\s+)?(?:files|contents|items|folder|directory)?\s*", "", _polite(text), flags=re.I)
        raw = re.sub(r"^(?:in|of|inside)\s+", "", raw, flags=re.I).strip()
        if not raw:
            return self._clarify("fs.list", "Which folder should I list?")
        path = self._path_or_clarify(raw, "fs.list", must_exist=True, default=self._default_read_root())
        if isinstance(path, ParseResult):
            return path
        return self._plan("fs.list", [self._step("fs.list", {"path": str(path)}, f"List {path.name or str(path)}")])

    def _h_read(self, text: str) -> ParseResult:
        raw = re.sub(r"^(?:read|open|show|display)\s+(?:the\s+)?(?:file|document)?\s*", "", _polite(text), flags=re.I).strip()
        raw = re.sub(r"^(?:contents?\s+of|text\s+of)\s+", "", raw, flags=re.I)
        if not raw:
            return self._clarify("fs.read", "Which file should I read?")
        path = self._path_or_clarify(raw, "fs.read", must_exist=True, default=self._default_read_root())
        if isinstance(path, ParseResult):
            return path
        return self._plan("fs.read", [self._step("fs.read", {"path": str(path)}, f"Read {path.name}")])

    def _h_write(self, text: str) -> ParseResult:
        command = _polite(text)
        overwrite = bool(re.search(r"\b(?:overwrite|replace)\b", command, re.I))
        content = ""
        raw_path = ""
        # write CONTENT to PATH
        match = re.match(r"^(?:write|put|save)\s+(?P<content>.+?)\s+(?:to|into)\s+(?P<path>.+)$", command, re.I)
        if match and not re.match(r"^(?:a\s+)?(?:new\s+)?(?:text\s+|word\s+|docx\s+)?file\b", match.group("content"), re.I):
            content = _strip_quotes(match.group("content"))
            raw_path = match.group("path")
        else:
            match = re.match(
                r"^(?:create|make|write|save)\s+(?:a\s+|an\s+)?(?:new\s+)?(?:text\s+|word\s+|docx\s+)?file"
                r"\s+(?:called\s+|named\s+|at\s+)?"
                r"(?P<path>\"[^\"]+\"|'[^']+'|\S+?)"
                r"(?:\s+(?:in|inside|under)\s+(?P<loc>.+?))?"
                r"(?:\s+(?:with|containing|saying)\s+(?:the\s+)?(?:content|text)?\s*(?P<content>.+))?$",
                command,
                re.I,
            )
            if match:
                raw_path = (match.group("path") or "").strip()
                content = (match.group("content") or "").strip()
                loc = (match.group("loc") or "").strip()
                if raw_path and loc and not re.match(r"^[A-Za-z]:[\\/]", raw_path.strip("\"'")):
                    raw_path = f"{loc.rstrip('\\/')}\\{raw_path.strip(chr(34) + chr(39))}"
            else:
                match = re.match(
                    r"^(?:create|make)\s+(?:a\s+|an\s+)?(?:new\s+)?(?:text\s+|word\s+|docx\s+)?file"
                    r"\s+(?:in|inside|under)\s+(?P<loc>.+?)\s+(?:called|named)\s+(?P<name>\"[^\"]+\"|'[^']+'|\S+)"
                    r"(?:\s+(?:with|containing|saying)\s+(?:the\s+)?(?:content|text)?\s*(?P<content>.+))?$",
                    command,
                    re.I,
                )
                if match:
                    raw_path = f"{match.group('loc').rstrip('\\/')}\\{match.group('name').strip(chr(34) + chr(39))}"
                    content = (match.group("content") or "").strip()
        if not raw_path:
            return self._clarify(
                "fs.write",
                "Tell me the file path and content. Example: create a file notes.txt in sandbox with content Hello.",
            )
        content = _strip_quotes(content)
        if not content and not re.search(r"\b(?:empty|blank)\b", command, re.I):
            return self._clarify("fs.write", "What content should the file contain?")
        path = self._path_or_clarify(raw_path, "fs.write", for_write=True)
        if isinstance(path, ParseResult):
            return path
        if path.suffix.lower() == ".pdf":
            return self._clarify("fs.write", "PDF writing is not supported. Use .txt, .md, or .docx.")
        return self._plan(
            "fs.write",
            [self._step(
                "fs.write",
                {"path": str(path), "content": content, "overwrite": overwrite, "create_parents": True},
                f"Write {path.name}",
            )],
        )

    def _h_create_folder(self, text: str) -> ParseResult:
        raw = re.sub(r"^(?:create|make)\s+(?:a\s+|an\s+)?(?:new\s+)?(?:folder|directory)\s*", "", _polite(text), flags=re.I)
        raw = re.sub(r"^(?:called|named|at)\s+", "", raw, flags=re.I).strip()
        if not raw:
            return self._clarify("fs.create_folder", "Which folder should I create?")
        head, location = self._location(raw)
        if location:
            head = head.strip().strip("\"'")
            if not re.match(r"^[A-Za-z]:[\\/]", head) and not head.lower().startswith(("sandbox", "downloads", "documents", "desktop", "project")):
                raw = f"{location.rstrip('\\/')}\\{head}"
            else:
                raw = head
        path = self._path_or_clarify(raw, "fs.create_folder", for_write=True)
        if isinstance(path, ParseResult):
            return path
        return self._plan(
            "fs.create_folder",
            [self._step("fs.create_folder", {"path": str(path)}, f"Create folder {path.name}")],
        )

    def _h_copy(self, text: str) -> ParseResult:
        match = re.match(r"^(?:copy|duplicate)\s+(?P<src>.+?)\s+(?:to|into)\s+(?P<dst>.+)$", _polite(text), re.I)
        if not match:
            return self._clarify("fs.copy", "Use: copy SOURCE to DESTINATION")
        overwrite = bool(re.search(r"\b(?:overwrite|replace)\b", text, re.I))
        src = self._path_or_clarify(match.group("src"), "fs.copy", must_exist=True, default=self._default_read_root())
        if isinstance(src, ParseResult):
            return src
        dst_raw = re.sub(r"\b(?:overwrite|replace)\b", "", match.group("dst"), flags=re.I).strip()
        dst = self._path_or_clarify(dst_raw, "fs.copy", for_write=True)
        if isinstance(dst, ParseResult):
            return dst
        if dst.is_dir():
            dst = dst / src.name
        return self._plan(
            "fs.copy",
            [self._step(
                "fs.copy",
                {"source": str(src), "destination": str(dst), "overwrite": overwrite},
                f"Copy {src.name} to {dst.name}",
            )],
        )

    def _h_move(self, text: str) -> ParseResult:
        match = re.match(r"^(?:move|transfer)\s+(?P<src>.+?)\s+(?:to|into)\s+(?P<dst>.+)$", _polite(text), re.I)
        if not match:
            return self._clarify("fs.move", "Use: move SOURCE to DESTINATION")
        overwrite = bool(re.search(r"\b(?:overwrite|replace)\b", text, re.I))
        src = self._path_or_clarify(match.group("src"), "fs.move", must_exist=True, default=self._default_read_root())
        if isinstance(src, ParseResult):
            return src
        dst_raw = re.sub(r"\b(?:overwrite|replace)\b", "", match.group("dst"), flags=re.I).strip()
        dst = self._path_or_clarify(dst_raw, "fs.move", for_write=True)
        if isinstance(dst, ParseResult):
            return dst
        if dst.is_dir():
            dst = dst / src.name
        return self._plan(
            "fs.move",
            [self._step(
                "fs.move",
                {"source": str(src), "destination": str(dst), "overwrite": overwrite},
                f"Move {src.name} to {dst.name}",
            )],
        )

    def _h_rename(self, text: str) -> ParseResult:
        match = re.match(r"^rename\s+(?P<src>.+?)\s+(?:to|as)\s+(?P<name>.+)$", _polite(text), re.I)
        if not match:
            return self._clarify("fs.rename", "Use: rename PATH to NEW_NAME")
        src = self._path_or_clarify(match.group("src"), "fs.rename", must_exist=True, default=self._default_read_root())
        if isinstance(src, ParseResult):
            return src
        name = _strip_quotes(match.group("name").strip())
        return self._plan(
            "fs.rename",
            [self._step("fs.rename", {"path": str(src), "new_name": name}, f"Rename {src.name}")],
        )

    def _h_delete(self, text: str) -> ParseResult:
        raw = re.sub(
            r"^(?:delete|remove|trash|quarantine)\s+(?:the\s+)?(?:file\s+|folder\s+|item\s+)?",
            "",
            _polite(text),
            flags=re.I,
        ).strip()
        if not raw:
            return self._clarify("fs.quarantine_delete", "Which file or folder should I quarantine?")
        parts = _split_quoted_list(raw)
        if len(parts) == 1:
            head, location = self._location(parts[0])
            if location:
                name = head.strip().strip("\"'")
                if not re.match(r"^[A-Za-z]:[\\/]", name) and not name.lower().startswith(
                    ("sandbox", "downloads", "documents", "desktop", "project")
                ):
                    parts = [f"{location.rstrip('\\/')}\\{name}"]
                else:
                    parts = [name]
        paths = []
        for part in parts:
            resolved = self._path_or_clarify(part, "fs.quarantine_delete", for_write=True, must_exist=True)
            if isinstance(resolved, ParseResult):
                return resolved
            paths.append(str(resolved))
        return self._plan(
            "fs.quarantine_delete",
            [self._step(
                "fs.quarantine_delete",
                {"paths": paths},
                f"Quarantine {len(paths)} item(s)",
            )],
        )

    def _h_zip(self, text: str) -> ParseResult:
        raw = re.sub(r"^(?:zip|compress|archive)\s+", "", _polite(text), flags=re.I).strip()
        match = re.match(r"^(?P<src>.+?)(?:\s+(?:to|into|as)\s+(?P<dst>.+))?$", raw, re.I)
        if not match:
            return self._clarify("fs.zip", "Use: zip SOURCE to DESTINATION.zip")
        src = self._path_or_clarify(match.group("src"), "fs.zip", must_exist=True, default=self._default_read_root())
        if isinstance(src, ParseResult):
            return src
        overwrite = bool(re.search(r"\b(?:overwrite|replace)\b", text, re.I))
        if match.group("dst"):
            dst = self._path_or_clarify(match.group("dst"), "fs.zip", for_write=True)
        else:
            dst = self._write_root() / f"{src.name}.zip"
        if isinstance(dst, ParseResult):
            return dst
        if dst.is_dir():
            dst = dst / f"{src.name}.zip"
        if dst.suffix.lower() != ".zip":
            dst = dst.with_suffix(dst.suffix + ".zip") if dst.suffix else dst.with_suffix(".zip")
        return self._plan(
            "fs.zip",
            [self._step("fs.zip", {"source": str(src), "destination": str(dst), "overwrite": overwrite}, f"Zip {src.name}")],
        )

    def _h_extract(self, text: str) -> ParseResult:
        raw = re.sub(r"^(?:unzip|extract)\s+", "", _polite(text), flags=re.I).strip()
        match = re.match(r"^(?P<archive>.+?)(?:\s+(?:to|into)\s+(?P<dst>.+))?$", raw, re.I)
        if not match:
            return self._clarify("fs.extract", "Use: extract ARCHIVE.zip to NEW_FOLDER")
        archive = self._path_or_clarify(match.group("archive"), "fs.extract", must_exist=True, default=self._default_read_root())
        if isinstance(archive, ParseResult):
            return archive
        if match.group("dst"):
            dst = self._path_or_clarify(match.group("dst"), "fs.extract", for_write=True)
        else:
            dst = self._write_root() / archive.stem
        if isinstance(dst, ParseResult):
            return dst
        return self._plan(
            "fs.extract",
            [self._step("fs.extract", {"archive": str(archive), "destination": str(dst)}, f"Extract {archive.name}")],
        )

    def _h_duplicates(self, text: str) -> ParseResult:
        raw = _polite(text)
        match = re.search(r"\b(?:duplicates?|same files?)\b(?:\s+(?:in|inside|under|of)\s+(.+))?$", raw, re.I)
        location = match.group(1) if match and match.group(1) else None
        if location is None:
            _head, location = self._location(raw)
            if not match:
                location = None
        path = self._path_or_clarify(location or str(self._default_read_root()), "fs.duplicates", must_exist=True, default=self._default_read_root())
        if isinstance(path, ParseResult):
            return path
        return self._plan(
            "fs.duplicates",
            [self._step("fs.duplicates", {"path": str(path), "min_size": 1}, f"Find duplicates in {path.name or str(path)}")],
        )

    def _h_inventory(self, text: str) -> ParseResult:
        raw = _polite(text)
        match = re.search(r"\binventory\b(?:\s+(?:of|for|in)\s+(.+))?$", raw, re.I)
        location = match.group(1) if match and match.group(1) else None
        path = self._path_or_clarify(location or str(self._default_read_root()), "fs.inventory", must_exist=True, default=self._default_read_root())
        if isinstance(path, ParseResult):
            return path
        output_raw = None
        out_match = re.search(r"\b(?:save|write|output)\s+(?:it\s+)?(?:to|as)\s+(.+)$", raw, re.I)
        if out_match:
            output_raw = out_match.group(1)
        args: dict[str, Any] = {"path": str(path)}
        if output_raw:
            output = self._path_or_clarify(output_raw, "fs.inventory", for_write=True)
            if isinstance(output, ParseResult):
                return output
            args["output"] = str(output)
        return self._plan(
            "fs.inventory",
            [self._step("fs.inventory", args, f"Inventorize {path.name or str(path)}")],
        )

    def _h_undo(self, text: str) -> ParseResult:
        return self._plan("fs.undo_last", [self._step("fs.undo_last", {}, "Undo the last supported change")])

    # Handlers: applications/windows ----------------------------------------
    def _h_launch(self, text: str) -> ParseResult:
        command = _polite(text)
        match = re.match(r"^(?:open|launch|start|run)\s+(?:the\s+)?(?P<target>.+)$", command, re.I)
        if not match:
            candidate = self._file_candidate(command)
            target = candidate or command
        else:
            target = match.group("target")
        target = self._target(target)
        if not target:
            return self._clarify("apps.launch", "Which application or file should I open?")
        url = re.match(r"^(?:https?|file)://", target, re.I)
        if url:
            return self._h_browser(target)
        # Existing files and folders are opened without reading their content.
        try:
            candidate = self._resolve(target, must_exist=True)
        except ValidationError:
            candidate = None
        if candidate is not None and candidate.exists():
            if candidate.is_dir():
                return self._plan(
                    "windows.open_folder",
                    [self._step("windows.open_folder", {"path": str(candidate)}, f"Open folder {candidate.name}")],
                )
            if candidate.suffix.lower() in {".txt", ".md", ".json", ".csv", ".log", ".pdf", ".docx"}:
                return self._plan(
                    "fs.read",
                    [self._step("fs.read", {"path": str(candidate)}, f"Read {candidate.name}")],
                )
            return self._plan(
                "browser.open",
                [self._step("browser.open", {"url": candidate.as_uri()}, f"Open {candidate.name} in its default app")],
            )
        if re.search(r"\b(?:folder|directory)\b", target, re.I):
            return self._h_open_folder(target)
        return self._plan(
            "apps.launch",
            [self._step("apps.launch", {"app": target}, f"Launch {target}")],
        )

    def _h_open_folder(self, text: str) -> ParseResult:
        command = _polite(text)
        match = re.match(r"^(?:open|show)\s+(?:the\s+)?(?:folder|directory)\s+(?P<path>.+)$", command, re.I)
        if not match:
            match = re.match(r"^(?:open|show)\s+(?P<path>.+?)\s+(?:folder|directory)$", command, re.I)
        if not match:
            return self._clarify("windows.open_folder", "Which folder should I open?")
        path = self._path_or_clarify(match.group("path"), "windows.open_folder", must_exist=True, default=self._default_read_root())
        if isinstance(path, ParseResult):
            return path
        return self._plan(
            "windows.open_folder",
            [self._step("windows.open_folder", {"path": str(path)}, f"Open folder {path.name}")],
        )

    def _h_list_windows(self, text: str) -> ParseResult:
        return self._plan("apps.list_windows", [self._step("apps.list_windows", {}, "List visible windows")])

    def _h_installed(self, text: str) -> ParseResult:
        return self._plan("apps.list_installed", [self._step("apps.list_installed", {}, "List installed applications")])

    def _h_app_capabilities(self, text: str) -> ParseResult:
        return self._plan("apps.capabilities", [self._step("apps.capabilities", {}, "Application capabilities")])

    def _h_focus(self, text: str) -> ParseResult:
        match = re.match(r"^(?:switch to|focus|bring)\s+(?P<title>.+?)(?:\s+(?:window|app|to the front))?$", _polite(text), re.I)
        if not match:
            return self._clarify("apps.focus_window", "Which window should I focus?")
        title = self._target(match.group("title"))
        if not title:
            return self._clarify("apps.focus_window", "Which window should I focus?")
        return self._plan(
            "apps.focus_window",
            [self._step("apps.focus_window", {"title": title, "exact": False}, f"Focus {title}")],
        )

    def _h_close(self, text: str) -> ParseResult:
        match = re.match(r"^(?:close|terminate|force close|force quit)\s+(?:the\s+)?(?P<name>.+)$", _polite(text), re.I)
        if not match:
            return self._clarify("apps.close", "Which application should I close?")
        name = re.sub(r"\bforce(?:fully)?\b", "", match.group("name"), flags=re.I).strip()
        force = bool(re.search(r"\bforce", text, re.I))
        name = self._target(name)
        if not name:
            return self._clarify("apps.close", "Which application should I close?")
        return self._plan(
            "apps.close",
            [self._step("apps.close", {"name": name, "force": force}, f"Close {name}" + (" (force)" if force else ""))],
        )

    def _h_type(self, text: str) -> ParseResult:
        command = _polite(text)
        match = re.match(r"^type\s+(?P<text>.+?)(?:\s+in(?:to)?\s+(?:the\s+)?(?P<window>.+?)(?:\s+window)?)?$", command, re.I)
        if not match:
            return self._clarify("apps.type_text", "Use: type TEXT in WINDOW")
        content = _strip_quotes(match.group("text").strip())
        window = (match.group("window") or "").strip()
        window = self._target(window) if window else ""
        if not content:
            return self._clarify("apps.type_text", "What text should I type?")
        args: dict[str, Any] = {"text": content}
        if window:
            args["window_title"] = window
        return self._plan(
            "apps.type_text",
            [self._step("apps.type_text", args, f"Type text" + (f" into {window}" if window else ""))],
        )

    # Handlers: Windows settings/status ------------------------------------
    def _h_settings(self, text: str) -> ParseResult:
        low = _norm(text)
        page = "home"
        for key, value in self._settings_pages.items():
            if re.search(r"\b" + re.escape(key) + r"\b", low):
                page = value
                break
        return self._plan(
            "windows.open_settings",
            [self._step("windows.open_settings", {"page": page}, f"Open Windows Settings ({page})")],
        )

    def _h_display(self, text: str) -> ParseResult:
        return self._plan("windows.display_info", [self._step("windows.display_info", {}, "Display information")])

    def _h_network(self, text: str) -> ParseResult:
        return self._plan("windows.network_status", [self._step("windows.network_status", {}, "Network status")])

    def _h_bluetooth(self, text: str) -> ParseResult:
        return self._plan("windows.bluetooth_status", [self._step("windows.bluetooth_status", {}, "Bluetooth status")])

    def _h_power(self, text: str) -> ParseResult:
        return self._plan("windows.power_status", [self._step("windows.power_status", {}, "Power status")])

    def _h_startup(self, text: str) -> ParseResult:
        return self._plan("windows.startup_list", [self._step("windows.startup_list", {}, "Startup entries")])

    # Handlers: system ------------------------------------------------------
    def _h_system_info(self, text: str) -> ParseResult:
        return self._plan("system.info", [self._step("system.info", {}, "System information")])

    def _h_processes(self, text: str) -> ParseResult:
        low = _norm(text)
        sort = "cpu" if re.search(r"\b(?:cpu|processor)\b", low) else "memory"
        match = re.search(r"\b(?:top|first)\s+(\d+)\b", low)
        limit = min(max(_int(match.group(1) if match else None, 10), 1), 30)
        return self._plan(
            "system.top_processes",
            [self._step("system.top_processes", {"sort": sort, "limit": limit}, f"Top processes by {sort}")],
        )

    def _h_media(self, text: str) -> ParseResult:
        low = _norm(text)
        action = None
        for phrase, value in (
            ("volume up", "volume_up"), ("turn up", "volume_up"), ("louder", "volume_up"),
            ("volume down", "volume_down"), ("turn down", "volume_down"), ("quieter", "volume_down"),
            ("unmute", "mute"), ("mute", "mute"),
            ("next track", "next"), ("next song", "next"), ("skip", "next"),
            ("previous track", "previous"), ("previous song", "previous"), ("go back", "previous"),
            ("play pause", "play_pause"), ("play/pause", "play_pause"), ("pause", "play_pause"), ("play", "play_pause"),
            ("stop", "stop"),
        ):
            if phrase in low:
                action = value
                break
        if action is None:
            return self._clarify("system.media_control", "Which media action? play/pause, next, previous, mute, volume up, or volume down.")
        return self._plan(
            "system.media_control",
            [self._step("system.media_control", {"action": action}, f"Media: {action}")],
        )

    def _h_battery(self, text: str) -> ParseResult:
        return self._plan("system.battery", [self._step("system.battery", {}, "Battery status")])

    def _h_audio(self, text: str) -> ParseResult:
        return self._plan("system.audio_devices", [self._step("system.audio_devices", {}, "Audio devices")])

    def _h_offline(self, text: str) -> ParseResult:
        return self._plan("system.offline_status", [self._step("system.offline_status", {}, "Offline enforcement status")])

    # Handlers: desktop -----------------------------------------------------
    def _h_clipboard_get(self, text: str) -> ParseResult:
        return self._plan("desktop.clipboard_get", [self._step("desktop.clipboard_get", {}, "Read clipboard text")])

    def _h_clipboard_set(self, text: str) -> ParseResult:
        command = _polite(text)
        content = ""
        match = re.match(r"^(?:copy|put|write)\s+(?P<text>.+?)\s+(?:to|on|onto)\s+(?:the\s+)?clipboard$", command, re.I)
        if match:
            content = match.group("text")
        else:
            match = re.match(r"^set\s+(?:the\s+)?clipboard\s+(?:to\s+)?(?P<text>.+)$", command, re.I)
            if match:
                content = match.group("text")
        content = _strip_quotes(content.strip())
        content = re.sub(r"^this\s+text\s*:\s*", "", content, flags=re.I)
        if not content:
            return self._clarify("desktop.clipboard_set", "What text should I put on the clipboard?")
        return self._plan(
            "desktop.clipboard_set",
            [self._step("desktop.clipboard_set", {"text": content}, "Set clipboard text")],
        )

    def _h_screenshot(self, text: str) -> ParseResult:
        command = _polite(text)
        match = re.search(r"(?:save|write|store)\s+(?:it\s+)?(?:to|as|at)\s+(?P<path>.+)$", command, re.I)
        if match:
            path = self._path_or_clarify(match.group("path"), "desktop.screenshot", for_write=True)
        else:
            path = self._write_root() / "screenshots" / f"screenshot_{_now_stamp()}.png"
        if isinstance(path, ParseResult):
            return path
        overwrite = bool(re.search(r"\b(?:overwrite|replace)\b", command, re.I))
        return self._plan(
            "desktop.screenshot",
            [self._step(
                "desktop.screenshot",
                {"path": str(path), "overwrite": overwrite},
                f"Capture screenshot to {path.name}",
            )],
        )

    # Handlers: developer ---------------------------------------------------
    def _h_dev_run(self, text: str) -> ParseResult:
        low = _norm(text)
        kind = "pytest"
        if "unittest" in low:
            kind = "unittest"
        elif "compile" in low:
            kind = "compileall"
        elif "git status" in low or low.strip() == "status":
            kind = "git_status"
        elif "git log" in low:
            kind = "git_log"
        elif "git diff" in low:
            kind = "git_diff"
        match = re.search(r"\s+in\s+(.+)$", _polite(text), re.I)
        project = self._path_or_clarify(match.group(1), "dev.run", for_write=True) if match else self.config.root
        if isinstance(project, ParseResult):
            return project
        return self._plan(
            "dev.run",
            [self._step("dev.run", {"project": str(project), "command": kind, "timeout_seconds": 120}, f"Run developer command ({kind})")],
        )

    def _h_log(self, text: str) -> ParseResult:
        candidate = self._file_candidate(_polite(text))
        match = re.search(r"(?:log|file)\s+(?:at\s+)?(.+)$", _polite(text), re.I)
        raw = match.group(1) if match else candidate
        if not raw:
            return self._clarify("dev.analyze_log", "Which log file should I analyze?")
        path = self._path_or_clarify(raw, "dev.analyze_log", must_exist=True, default=self._default_read_root())
        if isinstance(path, ParseResult):
            return path
        return self._plan(
            "dev.analyze_log",
            [self._step("dev.analyze_log", {"path": str(path), "max_lines": 60}, f"Analyze {path.name}")],
        )

    def _h_dev_status(self, text: str) -> ParseResult:
        return self._plan("dev.status", [self._step("dev.status", {}, "Developer command status")])

    # Handlers: browser/voice ----------------------------------------------
    def _h_browser(self, text: str) -> ParseResult:
        match = re.search(r"((?:https?|file)://\S+)", _polite(text), re.I)
        if not match:
            return self._clarify("browser.open", "Which URL should I open?")
        url = match.group(1).rstrip(".,;)")
        parsed = urlparse(url)
        if parsed.scheme.lower() not in {"http", "https", "file"}:
            return self._clarify("browser.open", "Only http, https, and file URLs are supported.")
        return self._plan(
            "browser.open",
            [self._step("browser.open", {"url": url}, f"Open {parsed.netloc or parsed.path}")],
        )

    def _h_voice_status(self, text: str) -> ParseResult:
        return self._plan("voice.status", [self._step("voice.status", {}, "Voice status")])

    def _h_voice_devices(self, text: str) -> ParseResult:
        return self._plan("voice.list_devices", [self._step("voice.list_devices", {}, "List microphones and speakers")])

    def _h_speak(self, text: str) -> ParseResult:
        raw = re.sub(r"^(?:speak|say)\s+", "", _polite(text), flags=re.I).strip()
        raw = _strip_quotes(raw)
        if not raw:
            return self._clarify("voice.speak", "What should I say?")
        return self._plan("voice.speak", [self._step("voice.speak", {"text": raw}, "Speak locally")])

    def _h_voice_capabilities(self, text: str) -> ParseResult:
        return self._plan("voice.capabilities", [self._step("voice.capabilities", {}, "Voice capabilities")])

    # Model fallback helpers expose handlers for tests/training coverage.
    def parse_intent_label(self, label: str, text: str) -> ParseResult:
        handler = self._handlers.get(label)
        if handler is None:
            return self._clarify(label, "That intent is not implemented.")
        return handler(text)
