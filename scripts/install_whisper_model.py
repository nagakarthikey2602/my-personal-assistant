"""Download the optional local faster-whisper model once.

The running assistant never downloads a model. This script is the only
supported way to fetch the weights. After it finishes, recognition is entirely
local: the model is loaded from ``models/whisper`` and huggingface_hub is put in
offline mode before the model is opened.

Run from the repository root:

    .venv\\Scripts\\python.exe scripts\\install_whisper_model.py
    .venv\\Scripts\\python.exe scripts\\install_whisper_model.py --check
    .venv\\Scripts\\python.exe scripts\\install_whisper_model.py --model base.en
    .venv\\Scripts\\python.exe scripts\\install_whisper_model.py --force

The default ``tiny.en`` weights are about 75 MB. ``base.en`` is slower but more
accurate on difficult audio.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "tiny.en"
DEFAULT_REPO = "Systran/faster-whisper-{model}"
REQUIRED_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")
ALLOW_PATTERNS = [
    "config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.txt",
    "preprocessor_config.json",
]


def model_ready(path: Path) -> bool:
    return all((path / name).is_file() for name in REQUIRED_FILES)


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def install(args: argparse.Namespace) -> int:
    model_dir = Path(args.dest).expanduser().resolve() if args.dest else ROOT / "models" / "whisper"
    short_name = str(args.model).strip()
    if short_name.startswith("Systran/"):
        repo_id = short_name
        short_name = repo_id.rsplit("/", 1)[-1]
        if short_name.startswith("faster-whisper-"):
            short_name = short_name[len("faster-whisper-"):]
    else:
        repo_id = DEFAULT_REPO.format(model=short_name)
    target = model_dir / f"faster-whisper-{short_name}"

    if args.check:
        if model_ready(target):
            print(f"Whisper model is installed: {target}")
            return 0
        print(f"Whisper model is not installed at {target}")
        print("Run this script without --check to download it once.")
        return 1

    if model_ready(target) and not args.force:
        print(f"Whisper model is already installed: {target}")
        print("Use --force to download and replace it.")
        return 0

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        raise SystemExit(
            "huggingface_hub is required to install the model. "
            "Run scripts\\setup.ps1 or pip install faster-whisper first."
        )

    if target.exists() and args.force:
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    print(f"Downloading {repo_id} (one time; not performed by the running assistant)")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    try:
        snapshot_download(
            repo_id=repo_id,
            local_dir=str(target),
            allow_patterns=ALLOW_PATTERNS,
            local_files_only=False,
        )
    except Exception as exc:
        raise SystemExit(f"Download failed: {exc.__class__.__name__}: {exc}") from exc

    if not model_ready(target):
        missing = [name for name in REQUIRED_FILES if not (target / name).is_file()]
        raise SystemExit(f"The download did not produce the expected files (missing {', '.join(missing)}).")

    # The cache metadata is not needed at runtime and only adds clutter.
    cache = target / ".cache"
    if cache.is_dir():
        shutil.rmtree(cache, ignore_errors=True)

    print(f"Whisper model installed: {target} ({human_size(directory_size(target))})")
    print("Next: set speech.asr_engine: whisper in config/config.yaml for push-to-talk,")
    print("or keep speech.asr_engine: vosk and let wake-word commands use Whisper.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default=DEFAULT_MODEL, help="tiny.en, base.en, small.en, or a full repo id.")
    parser.add_argument("--dest", help="Model parent directory (default: models/whisper).")
    parser.add_argument("--force", action="store_true", help="Replace an existing model.")
    parser.add_argument("--check", action="store_true", help="Only report whether the model is installed.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return install(args)


if __name__ == "__main__":
    sys.exit(main())
