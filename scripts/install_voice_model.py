"""Download and install the optional local Vosk English speech model once.

This script is the only supported way to fetch the model. The application never
downloads a model at runtime. Vosk and the Kaldi model are Apache-2.0 licensed;
after this one-time download all recognition runs on this PC with no cloud
fallback.

Run from the repository root:

    .venv\\Scripts\\python.exe scripts\\install_voice_model.py
    .venv\\Scripts\\python.exe scripts\\install_voice_model.py --check
    .venv\\Scripts\\python.exe scripts\\install_voice_model.py --force

The default ``vosk-model-small-en-us-0.15`` archive is about 40 MB and expands
to about 40 MB. Set ``speech.asr_engine: vosk`` and optionally
``speech.wake_word_enabled: true`` in config/config.yaml after installing.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "vosk-model-small-en-us-0.15"
DEFAULT_BASE_URL = "https://alphacephei.com/vosk/models"
MIN_ARCHIVE_BYTES = 30_000_000


def model_ready(path: Path) -> bool:
    return (path / "am").is_dir() and (path / "conf").is_dir() and (path / "graph").is_dir()


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def download(url: str, destination: Path, *, force: bool) -> None:
    if destination.exists() and not force and destination.stat().st_size >= MIN_ARCHIVE_BYTES:
        print(f"Using existing archive: {destination}")
        return

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    print(f"Downloading {url}")
    print("This is a one-time download; it is not performed by the running assistant.")
    request = urllib.request.Request(url, headers={"User-Agent": "LocalPersonalAssistant/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            last_report = -1
            with temporary.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    handle.write(chunk)
                    done += len(chunk)
                    if total:
                        percent = int(done * 100 / total)
                        if percent >= last_report + 5:
                            last_report = percent
                            print(f"  {percent:3d}%  {human_size(done)} / {human_size(total)}")
                    elif done // (8 * 1024 * 1024) != last_report:
                        last_report = done // (8 * 1024 * 1024)
                        print(f"  {human_size(done)}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        temporary.unlink(missing_ok=True)
        raise SystemExit(f"Download failed: {exc}") from exc

    if total and done != total:
        temporary.unlink(missing_ok=True)
        raise SystemExit(f"Download was incomplete ({done} of {total} bytes).")
    temporary.replace(destination)
    print(f"Saved {human_size(destination.stat().st_size)} to {destination}")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    root = destination.resolve()
    for member in archive.infolist():
        target = (destination / member.filename).resolve()
        if target != root and root not in target.parents:
            raise SystemExit(f"Archive contains an unsafe path: {member.filename}")
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as source, target.open("wb") as output:
            shutil.copyfileobj(source, output)


def install(args: argparse.Namespace) -> int:
    model_dir = Path(args.dest).expanduser().resolve() if args.dest else ROOT / "models" / "vosk"
    target = model_dir / args.model
    archive_path = model_dir / f"{args.model}.zip"

    if args.check:
        if model_ready(target):
            print(f"Vosk model is installed: {target}")
            return 0
        print(f"Vosk model is not installed at {target}")
        print("Run this script without --check to download it once.")
        return 1

    if model_ready(target) and not args.force:
        print(f"Vosk model is already installed: {target}")
        print("Use --force to download and replace it.")
        return 0

    url = args.url or f"{DEFAULT_BASE_URL}/{args.model}.zip"
    download(url, archive_path, force=args.force)
    print(f"Archive SHA256: {digest(archive_path)}")

    model_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".vosk-extract-", dir=model_dir) as temporary:
        temporary_path = Path(temporary)
        try:
            with zipfile.ZipFile(archive_path) as archive:
                safe_extract(archive, temporary_path)
        except zipfile.BadZipFile as exc:
            raise SystemExit(f"The downloaded file is not a valid zip archive: {exc}") from exc

        extracted = temporary_path / args.model
        if not model_ready(extracted):
            raise SystemExit(
                f"The archive did not contain the expected model layout under '{args.model}'."
            )
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(extracted), str(target))

    if not args.keep_archive:
        archive_path.unlink(missing_ok=True)
    print(f"Vosk model installed: {target}")
    print("Next: set speech.asr_engine: vosk in config/config.yaml.")
    print("For hands-free use, set speech.wake_word_enabled: true (microphone stays open).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Vosk model directory name.")
    parser.add_argument("--url", help="Override the model zip URL (must be HTTPS).")
    parser.add_argument("--dest", help="Model parent directory (default: models/vosk).")
    parser.add_argument("--force", action="store_true", help="Replace an existing model/archive.")
    parser.add_argument("--keep-archive", action="store_true", help="Keep the downloaded zip.")
    parser.add_argument("--check", action="store_true", help="Only report whether the model is installed.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.url and not args.url.lower().startswith("https://"):
        parser.error("--url must use HTTPS.")
    return install(args)


if __name__ == "__main__":
    sys.exit(main())
