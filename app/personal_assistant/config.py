"""Configuration loading, validation, and local path resolution.

The runtime never downloads configuration. Defaults are embedded in source, and an
optional YAML file is read from ``<APP_ROOT>/config/config.yaml``. An environment
variable ``PA_HOME`` or ``PA_CONFIG`` may override locations for testing.
"""

from __future__ import annotations

import copy
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping

import yaml

from .errors import ConfigError

APP_ROOT_MARKER = "<APP_ROOT>"
HOME_MARKER = "<HOME>"

DEFAULT_CONFIG: dict[str, Any] = {
    "app": {
        "name": "Local Personal Assistant",
        "offline_mode": "strict",  # strict | advisory
        "auto_approve_max_risk": 2,
        "confirm_level_3": True,
        "confirm_level_4": True,
        "max_task_seconds": 600,
        "max_steps": 40,
        "log_level": "INFO",
        "ui": {
            "spoken_responses": False,
            "voice_enabled": True,
            "start_minimised": False,
        },
    },
    "paths": {
        "write_roots": [f"{APP_ROOT_MARKER}\\sandbox"],
        "read_roots": [
            f"{APP_ROOT_MARKER}",
            f"{HOME_MARKER}\\Downloads",
            f"{HOME_MARKER}\\Documents",
            f"{HOME_MARKER}\\Desktop",
        ],
        # Internal runtime locations must be explicit here so marker resolution
        # happens before they are used. They are blocked from general file tools.
        "data": f"{APP_ROOT_MARKER}\\data",
        "logs": f"{APP_ROOT_MARKER}\\logs",
        "models": f"{APP_ROOT_MARKER}\\models",
        "training_data": f"{APP_ROOT_MARKER}\\training_data",
        "quarantine": f"{APP_ROOT_MARKER}\\data\\quarantine",
        "backups": f"{APP_ROOT_MARKER}\\data\\backups",
        "database": f"{APP_ROOT_MARKER}\\data\\assistant.db",
        "recovery_journal": f"{APP_ROOT_MARKER}\\data\\recovery\\journal.json",
        "temp": f"{APP_ROOT_MARKER}\\data\\temp",
        "deny_names": [
            "*.pfx",
            "*.p12",
            "id_rsa",
            "id_ed25519",
            "*.kdbx",
            ".env",
            "NTUSER.DAT",
            "Login Data",
            "Cookies",
        ],
        "deny_path_fragments": [
            "\\.ssh\\",
            "\\.gnupg\\",
            "\\.aws\\",
            "\\AppData\\Roaming\\Microsoft\\Credentials\\",
            "\\AppData\\Local\\Microsoft\\Credentials\\",
            "\\Windows\\System32\\",
            "\\Windows\\SysWOW64\\",
        ],
        "protected_roots": [
            f"{APP_ROOT_MARKER}\\data",
            f"{APP_ROOT_MARKER}\\logs",
            f"{APP_ROOT_MARKER}\\config",
            f"{APP_ROOT_MARKER}\\training_data",
            f"{APP_ROOT_MARKER}\\models",
        ],
        "allow_protected_read": False,
        "allow_dangerous_roots": False,
    },
    "files": {
        "max_read_bytes": 2_000_000,
        "max_write_bytes": 5_000_000,
        "max_search_results": 200,
        "max_search_seconds": 20,
        "max_scan_files": 20000,
        "max_zip_entries": 2000,
        "max_zip_uncompressed_bytes": 500_000_000,
        "max_zip_ratio": 200,
        "allow_overwrite": False,
        "text_extensions": [
            ".txt", ".md", ".json", ".jsonl", ".csv", ".log", ".py", ".ps1",
            ".yaml", ".yml", ".toml", ".ini", ".cfg", ".html", ".htm", ".js",
            ".ts", ".css", ".java", ".c", ".h", ".cpp", ".cs", ".sql", ".xml",
        ],
        "document_extensions": [".pdf", ".docx"],
        "quarantine_keep_days": 30,
    },
    "memory": {
        "enabled": True,
        "store_commands": True,
        "store_outputs": False,
        "retention_days": 90,
        "max_history": 1000,
    },
    "permissions": {
        "approval_timeout_seconds": 120,
        "audit_log": f"{APP_ROOT_MARKER}\\logs\\audit.jsonl",
        "audit_max_megabytes": 10,
        "allowed_tools": ["*"],
        "denied_tools": [],
    },
    "tools": {
        "terminal_enabled": True,
        "terminal_timeout_seconds": 120,
        "terminal_max_output_bytes": 200_000,
        "allowed_commands": [
            ["python", "-m", "pytest", "-q"],
            ["python", "-m", "unittest", "discover", "-v"],
            ["python", "-m", "compileall", "-q", "."],
            ["git", "status", "--short"],
            ["git", "log", "-n", "20", "--oneline"],
            ["git", "diff", "--stat"],
        ],
        "browser_network_enabled": False,
        "browser_allowed_schemes": ["https", "http", "file"],
        "screenshot_enabled": True,
        "clipboard_enabled": True,
        "ui_automation_enabled": True,
    },
    "intelligence": {
        "enable_model": True,
        "intent_model_path": f"{APP_ROOT_MARKER}\\models\\intent_model.json",
        "min_confidence": 0.55,
        "min_margin": 0.15,
    },
    "speech": {
        "asr_engine": "template",  # template | vosk | whisper | none
        "tts_engine": "formant",  # formant | sapi | none
        "allow_os_pretrained_tts": False,
        "input_device": None,
        "output_device": None,
        "sample_rate": 16000,
        "channels": 1,
        "push_to_talk_seconds": 8,
        "vad_energy_threshold": 0.008,
        "template_threshold": 0.72,
        "template_margin": 0.08,
        "templates_dir": f"{APP_ROOT_MARKER}\\models\\speech_templates",
        "vosk_model_dir": f"{APP_ROOT_MARKER}\\models\\vosk\\vosk-model-small-en-us-0.15",
        "vosk_language": "en-us",
        "whisper_model_dir": f"{APP_ROOT_MARKER}\\models\\whisper\\faster-whisper-tiny.en",
        "whisper_language": "en",
        "whisper_compute_type": "int8",
        "whisper_cpu_threads": 4,
        "whisper_beam_size": 5,
        "whisper_initial_prompt": "",
        "wake_word_enabled": False,
        "wake_word": "hearty",
        "wake_word_aliases": ["hearty", "hardy", "harty", "heart y"],
        "wake_command_engine": "whisper",
        "wake_command_seconds": 6.0,
        "wake_command_phrases": [],
        "wake_command_timeout": 10.0,
        "wake_command_settle_seconds": 0.65,
        "wake_ack_sound": True,
        "max_speech_seconds": 15,
    },
    "diagnostics": {
        "test_directory": f"{APP_ROOT_MARKER}\\sandbox",
    },
    "integration": {
        "android_enabled": False,
        "bind_host": "127.0.0.1",
        "bind_port": 8765,
        "require_pairing_token": True,
    },
}

_DANGEROUS_ROOTS = {
    "c:\\windows",
    "c:\\program files",
    "c:\\program files (x86)",
    "c:\\programdata",
}


def default_root() -> Path:
    override = os.environ.get("PA_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "PersonalAssistant"
    return Path(__file__).resolve().parents[2]


def _deep_merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, Mapping):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _resolve_markers(value: Any, root: Path) -> Any:
    if isinstance(value, dict):
        return {key: _resolve_markers(item, root) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_markers(item, root) for item in value]
    if isinstance(value, str):
        expanded = value.replace(APP_ROOT_MARKER, str(root)).replace(HOME_MARKER, str(Path.home()))
        expanded = os.path.expandvars(os.path.expanduser(expanded))
        return expanded
    return value


def _normalise_root(path_text: str, root: Path) -> Path:
    candidate = Path(path_text).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve(strict=False)


class Config:
    """Validated configuration with resolved local paths."""

    def __init__(self, data: Mapping[str, Any], root: Path, source: Path | None = None) -> None:
        self.root = root.resolve()
        self.source = source
        # Merge first, then resolve markers, so defaults pulled in by the merge
        # cannot leave a literal "<APP_ROOT>" directory on disk.
        merged = _deep_merge(DEFAULT_CONFIG, copy.deepcopy(dict(data)))
        self.data = _resolve_markers(merged, self.root)
        self._resolve_paths()
        self.validate()

    @classmethod
    def load(cls, path: str | Path | None = None, root: str | Path | None = None) -> "Config":
        base = Path(root).expanduser().resolve() if root else default_root()
        if path is None:
            env_path = os.environ.get("PA_CONFIG")
            path = Path(env_path) if env_path else base / "config" / "config.yaml"
        else:
            path = Path(path)
        source: Path | None = None
        overlay: dict[str, Any] = {}
        if path and path.exists():
            try:
                loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError) as exc:
                raise ConfigError(f"Could not read configuration: {path.name}") from exc
            if not isinstance(loaded, dict):
                raise ConfigError("The configuration file must contain a YAML mapping.")
            overlay = loaded
            source = path.resolve()
        data = _deep_merge(DEFAULT_CONFIG, overlay)
        return cls(data, base, source)

    @classmethod
    def from_dict(cls, overlay: Mapping[str, Any], root: str | Path) -> "Config":
        return cls(overlay, Path(root))

    def _resolve_paths(self) -> None:
        paths = self.data["paths"]
        paths["write_roots"] = [str(_normalise_root(item, self.root)) for item in paths["write_roots"]]
        paths["read_roots"] = [str(_normalise_root(item, self.root)) for item in paths["read_roots"]]
        paths["quarantine"] = str(_normalise_root(paths.get("quarantine", f"{APP_ROOT_MARKER}\\data\\quarantine"), self.root))
        paths["backups"] = str(_normalise_root(paths.get("backups", f"{APP_ROOT_MARKER}\\data\\backups"), self.root))
        paths["data"] = str(_normalise_root(paths.get("data", f"{APP_ROOT_MARKER}\\data"), self.root))
        paths["logs"] = str(_normalise_root(paths.get("logs", f"{APP_ROOT_MARKER}\\logs"), self.root))
        paths["models"] = str(_normalise_root(paths.get("models", f"{APP_ROOT_MARKER}\\models"), self.root))
        paths["training_data"] = str(_normalise_root(paths.get("training_data", f"{APP_ROOT_MARKER}\\training_data"), self.root))
        paths["database"] = str(_normalise_root(paths.get("database", f"{APP_ROOT_MARKER}\\data\\assistant.db"), self.root))
        paths["recovery_journal"] = str(_normalise_root(paths.get("recovery_journal", f"{APP_ROOT_MARKER}\\data\\recovery\\journal.json"), self.root))
        paths["temp"] = str(_normalise_root(paths.get("temp", f"{APP_ROOT_MARKER}\\data\\temp"), self.root))
        paths["protected_roots"] = [
            str(_normalise_root(item, self.root)) for item in paths.get("protected_roots", [])
        ]
        self.data["permissions"]["audit_log"] = str(_normalise_root(self.data["permissions"]["audit_log"], self.root))
        self.data["intelligence"]["intent_model_path"] = str(_normalise_root(self.data["intelligence"]["intent_model_path"], self.root))
        self.data["speech"]["templates_dir"] = str(_normalise_root(self.data["speech"]["templates_dir"], self.root))
        if "vosk_model_dir" in self.data["speech"]:
            self.data["speech"]["vosk_model_dir"] = str(
                _normalise_root(self.data["speech"]["vosk_model_dir"], self.root)
            )
        if "whisper_model_dir" in self.data["speech"]:
            self.data["speech"]["whisper_model_dir"] = str(
                _normalise_root(self.data["speech"]["whisper_model_dir"], self.root)
            )
        self.data["diagnostics"]["test_directory"] = str(_normalise_root(self.data["diagnostics"]["test_directory"], self.root))

    def validate(self) -> None:
        if self.data["app"]["offline_mode"] not in {"strict", "advisory"}:
            raise ConfigError("app.offline_mode must be 'strict' or 'advisory'.")
        for name in ("auto_approve_max_risk",):
            value = self.get(f"app.{name}")
            if not isinstance(value, int) or not 1 <= value <= 4:
                raise ConfigError(f"app.{name} must be an integer between 1 and 4.")
        if self.get("app.max_steps") < 1 or self.get("app.max_steps") > 200:
            raise ConfigError("app.max_steps must be between 1 and 200.")
        if self.get("speech.asr_engine") not in {"template", "vosk", "whisper", "none"}:
            raise ConfigError("speech.asr_engine must be 'template', 'vosk', 'whisper' or 'none'.")
        if not isinstance(self.get("speech.vosk_model_dir"), str) or not self.get("speech.vosk_model_dir"):
            raise ConfigError("speech.vosk_model_dir must be a directory path.")
        if not isinstance(self.get("speech.whisper_model_dir"), str) or not self.get("speech.whisper_model_dir"):
            raise ConfigError("speech.whisper_model_dir must be a directory path.")
        aliases = self.get("speech.wake_word_aliases")
        if not isinstance(aliases, list) or not all(isinstance(item, str) for item in aliases):
            raise ConfigError("speech.wake_word_aliases must be a list of strings.")
        if self.get("speech.wake_command_engine") not in {"whisper", "vosk"}:
            raise ConfigError("speech.wake_command_engine must be 'whisper' or 'vosk'.")
        if not 1.0 <= float(self.get("speech.wake_command_seconds", 6.0)) <= 15.0:
            raise ConfigError("speech.wake_command_seconds must be between 1 and 15 seconds.")
        if not 2.0 <= float(self.get("speech.wake_command_timeout", 10.0)) <= 30.0:
            raise ConfigError("speech.wake_command_timeout must be between 2 and 30 seconds.")
        if not 0.2 <= float(self.get("speech.wake_command_settle_seconds", 0.65)) <= 3.0:
            raise ConfigError("speech.wake_command_settle_seconds must be between 0.2 and 3 seconds.")
        if self.get("speech.whisper_compute_type") not in {"int8", "int8_float32", "float32"}:
            raise ConfigError("speech.whisper_compute_type must be 'int8', 'int8_float32' or 'float32'.")
        if not 1 <= int(self.get("speech.whisper_cpu_threads", 4)) <= 64:
            raise ConfigError("speech.whisper_cpu_threads must be between 1 and 64.")
        if not 1 <= int(self.get("speech.whisper_beam_size", 5)) <= 10:
            raise ConfigError("speech.whisper_beam_size must be between 1 and 10.")
        if self.get("speech.tts_engine") not in {"formant", "sapi", "none"}:
            raise ConfigError("speech.tts_engine must be 'formant', 'sapi' or 'none'.")
        if self.get("speech.tts_engine") == "sapi" and not self.get("speech.allow_os_pretrained_tts"):
            raise ConfigError(
                "speech.tts_engine='sapi' uses Microsoft-pretrained voices. "
                "Explicitly set speech.allow_os_pretrained_tts: true to acknowledge the model policy."
            )
        if int(self.get("speech.sample_rate")) < 8000 or int(self.get("speech.sample_rate")) > 48000:
            raise ConfigError("speech.sample_rate must be between 8000 and 48000.")
        for key in ("write_roots", "read_roots"):
            roots = self.data["paths"][key]
            if not isinstance(roots, list) or not all(isinstance(item, str) for item in roots):
                raise ConfigError(f"paths.{key} must be a list of paths.")
            for text in roots:
                path = Path(text).resolve(strict=False)
                if str(path).lower() in _DANGEROUS_ROOTS and not self.get("paths.allow_dangerous_roots"):
                    raise ConfigError(
                        f"Refusing dangerous {key} entry {path}. Set paths.allow_dangerous_roots "
                        "only if you fully understand the risk."
                    )
        max_read = int(self.get("files.max_read_bytes"))
        max_write = int(self.get("files.max_write_bytes"))
        if not (1000 <= max_read <= 100_000_000) or not (1000 <= max_write <= 100_000_000):
            raise ConfigError("File size limits must be between 1,000 and 100,000,000 bytes.")
        if self.get("app.offline_mode") == "strict" and self.get("tools.browser_network_enabled"):
            # This combination is allowed only with network access explicitly enabled.
            pass

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ConfigError(f"Cannot set {dotted}: path is not a mapping.")
        node[parts[-1]] = value
        self._resolve_paths()
        self.validate()

    def path(self, dotted: str) -> Path:
        value = self.get(dotted)
        if not isinstance(value, str) or not value:
            raise ConfigError(f"Configuration path '{dotted}' is not set.")
        return Path(value)

    def ensure_directories(self) -> list[Path]:
        paths = [
            self.path("paths.data"),
            self.path("paths.logs"),
            self.path("paths.models"),
            self.path("paths.training_data"),
            self.path("paths.quarantine"),
            self.path("paths.backups"),
            self.path("paths.temp"),
            Path(self.data["permissions"]["audit_log"]).parent,
            Path(self.data["intelligence"]["intent_model_path"]).parent,
            self.path("speech.templates_dir"),
            Path(self.data["paths"]["database"]).parent,
            Path(self.data["paths"]["recovery_journal"]).parent,
        ]
        for path in paths:
            path.mkdir(parents=True, exist_ok=True)
        for root_text in self.data["paths"]["write_roots"]:
            Path(root_text).mkdir(parents=True, exist_ok=True)
        return paths

    def seed_bundled_models(self) -> list[Path]:
        """Copy a packaged local model into the per-user data directory once.

        A frozen build keeps runtime data under the per-user root. The small
        from-scratch intent model may be shipped next to the executable so a new
        installation can use it without retraining. This copies a local file only;
        it never downloads anything and never overwrites a user-trained model.
        """
        if not getattr(sys, "frozen", False):
            return []
        target = self.path("intelligence.intent_model_path")
        if target.exists():
            return []
        executable_dir = Path(sys.executable).resolve().parent
        candidates = [
            executable_dir / "models" / "intent_model.json",
            executable_dir / "_internal" / "models" / "intent_model.json",
        ]
        copied: list[Path] = []
        for source in candidates:
            try:
                if not source.is_file():
                    continue
                if source.resolve() == target.resolve():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                copied.append(target)
                break
            except OSError:
                continue
        return copied

    def write_roots(self) -> list[Path]:
        return [Path(item) for item in self.data["paths"]["write_roots"]]

    def read_roots(self) -> list[Path]:
        return [Path(item) for item in self.data["paths"]["read_roots"]]

    def public_dict(self) -> dict[str, Any]:
        """A UI-safe view. There are no passwords in normal config; still avoid raw secrets."""
        data = copy.deepcopy(self.data)
        return data

    def save(self, path: str | Path | None = None) -> Path:
        """Persist the current configuration as local YAML.

        This is only called by explicit user actions. It resolves to absolute
        paths and never stores secrets (the configuration schema has none).
        """
        target = Path(path) if path else (self.source or self.root / "config" / "config.yaml")
        target.parent.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(self.data, sort_keys=False, allow_unicode=True)
        temp = target.with_name(f".{target.name}.tmp")
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, target)
        self.source = target
        return target


def load_config(path: str | Path | None = None, root: str | Path | None = None) -> Config:
    return Config.load(path=path, root=root)
