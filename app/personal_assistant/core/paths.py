"""Central path allowlist and canonicalisation.

All filesystem tools go through this policy. Canonicalisation resolves existing
symlinks/junctions before the allowlist check, which prevents a link inside an
allowed folder from escaping to a protected location.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from ..config import Config
from ..errors import PathDeniedError

_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *{f"com{i}" for i in range(1, 10)},
    *{f"lpt{i}" for i in range(1, 10)},
}
_ADS_RE = re.compile(r"^[A-Za-z]:")
_FORBIDDEN_MARKERS = ("\\windows\\system32\\", "\\windows\\syswow64\\")


def _norm(path: Path) -> str:
    return os.path.normcase(str(path)).rstrip("\\/")


def _inside(child: Path, parent: Path) -> bool:
    child_s = _norm(child)
    parent_s = _norm(parent)
    return child_s == parent_s or child_s.startswith(parent_s + os.sep)


class PathPolicy:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.read_roots = [Path(item).resolve(strict=False) for item in config.read_roots()]
        self.write_roots = [Path(item).resolve(strict=False) for item in config.write_roots()]
        # Write roots are implicitly readable.
        for root in self.write_roots:
            if root not in self.read_roots:
                self.read_roots.append(root)
        self.deny_names = [str(item).lower() for item in config.get("paths.deny_names", [])]
        self.deny_fragments = [
            str(item).lower().replace("/", "\\")
            for item in config.get("paths.deny_path_fragments", [])
        ]
        self.protected_roots = [
            Path(item).resolve(strict=False) for item in config.get("paths.protected_roots", [])
        ]
        self.allow_protected_read = bool(config.get("paths.allow_protected_read", False))

    def canonical(self, path: str | Path) -> Path:
        if not isinstance(path, (str, Path)):
            raise PathDeniedError("The path must be text.")
        text = str(path).strip().strip('"')
        if not text or "\x00" in text:
            raise PathDeniedError("The path is empty or contains invalid characters.")
        if text.startswith("\\\\"):
            raise PathDeniedError("UNC network paths are not permitted by the offline path policy.")
        if len(text) > 30000:
            raise PathDeniedError("The path is too long.")
        # Block NTFS alternate data streams. A normal drive colon is fine.
        if ":" in text and not _ADS_RE.match(text):
            raise PathDeniedError("Alternate data streams are not permitted.")
        if _ADS_RE.match(text) and ":" in text[2:]:
            raise PathDeniedError("Alternate data streams are not permitted.")
        try:
            candidate = Path(text).expanduser()
            if not candidate.is_absolute():
                candidate = self.config.root / candidate
            resolved = candidate.resolve(strict=False)
        except (OSError, ValueError) as exc:
            raise PathDeniedError("The path could not be resolved safely.") from exc
        if resolved.drive and not resolved.drive.endswith(":"):
            raise PathDeniedError("The path drive is invalid.")
        for part in resolved.parts:
            if part.lower() in _RESERVED_NAMES:
                raise PathDeniedError("Windows reserved device names cannot be used.")
        return resolved

    def check_read(self, path: str | Path) -> Path:
        resolved = self.canonical(path)
        self._check_denied(resolved, write=False)
        if not any(_inside(resolved, root) for root in self.read_roots):
            raise PathDeniedError(
                "Read access is limited to configured folders. "
                f"Allowed roots: {', '.join(str(root) for root in self.read_roots)}"
            )
        return resolved

    def check_write(self, path: str | Path) -> Path:
        resolved = self.canonical(path)
        self._check_denied(resolved, write=True)
        if not any(_inside(resolved, root) for root in self.write_roots):
            raise PathDeniedError(
                "Write access is limited to configured write roots. "
                f"Allowed write roots: {', '.join(str(root) for root in self.write_roots)}"
            )
        return resolved

    def _check_denied(self, path: Path, *, write: bool) -> None:
        # allow_protected_read is deliberately a read-only exception. Internal
        # runtime data is never writable through the general file tools, even if
        # the operator later adds an internal folder to a write root.
        if any(_inside(path, root) for root in self.protected_roots):
            if write or not self.allow_protected_read:
                raise PathDeniedError(
                    "Runtime memory, logs, configuration, training data, and local models are "
                    "private internal data and are not accessible through general file tools."
                )
        lowered = "\\" + str(path).lower().replace("/", "\\")
        for fragment in self.deny_fragments:
            normalised = fragment if fragment.startswith("\\") else "\\" + fragment
            if normalised.lower() in lowered:
                raise PathDeniedError("That location contains protected security data and is blocked.")
        for marker in _FORBIDDEN_MARKERS:
            if marker in lowered:
                raise PathDeniedError("Windows system directories are blocked by the assistant policy.")
        name = path.name.lower()
        for pattern in self.deny_names:
            if fnmatch.fnmatch(name, pattern):
                raise PathDeniedError("That file type or name is blocked by the sensitive-file policy.")
        # Also block denied names anywhere in the path (e.g. a .ssh folder).
        for part in path.parts:
            part_l = part.lower()
            for pattern in self.deny_names:
                if fnmatch.fnmatch(part_l, pattern):
                    raise PathDeniedError("That path contains a sensitive file or folder name.")

    def is_writable(self, path: str | Path) -> bool:
        try:
            self.check_write(path)
            return True
        except PathDeniedError:
            return False

    def describe(self) -> dict[str, list[str]]:
        return {
            "read_roots": [str(item) for item in self.read_roots],
            "write_roots": [str(item) for item in self.write_roots],
            "denied_names": self.deny_names,
            "protected_roots": [str(item) for item in self.protected_roots],
        }
