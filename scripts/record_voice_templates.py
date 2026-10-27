"""Record the user's own closed-vocabulary voice templates.

This script is interactive and only runs when the user explicitly invokes it.
It records from the default Windows microphone through WinMM, removes silence,
extracts MFCC features locally, and stores those features plus a manifest.
No audio is sent anywhere and no pretrained acoustic model is used.

The recognizer can only match the exact phrases recorded here. It is not
general dictation.

Run from the repository root:

    .venv\\Scripts\\python.exe scripts\\record_voice_templates.py --list
    .venv\\Scripts\\python.exe scripts\\record_voice_templates.py
    .venv\\Scripts\\python.exe scripts\\record_voice_templates.py --phrase "play some music"

Each phrase is recorded several times (default 2 takes). Speak normally at a
similar distance each time. You can delete a phrase later with --remove.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from personal_assistant.config import Config  # noqa: E402
from personal_assistant.speech.features import mfcc  # noqa: E402
from personal_assistant.speech.native_audio import AudioError, audio_status, record_audio  # noqa: E402
from personal_assistant.speech.vad import trim_silence  # noqa: E402
from personal_assistant.util import atomic_write_text  # noqa: E402

DEFAULT_PHRASES = [
    "help",
    "status",
    "take a screenshot",
    "open notepad",
    "list files in downloads",
    "system info",
    "battery status",
    "network status",
    "what do you remember",
    "undo",
    "stop",
    "kill switch",
]


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "version": 1,
            "provenance": "User-recorded voice templates. No pretrained acoustic model.",
            "sample_rate": None,
            "phrases": [],
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Could not read {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("phrases"), list):
        raise SystemExit(f"{path} is not a valid voice template manifest.")
    return data


def save_manifest(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def show_manifest(path: Path, data: dict[str, Any]) -> None:
    phrases = data.get("phrases", [])
    print(f"Template directory: {path.parent}")
    print(f"Phrases installed: {len(phrases)}")
    for phrase in phrases:
        takes = phrase.get("takes", [])
        print(f"  - {phrase.get('text')!r}: {len(takes)} take(s)")
    if not phrases:
        print("No voice templates are installed; voice input will report that honestly.")


def remove_phrase(path: Path, data: dict[str, Any], text: str) -> int:
    target = text.strip().lower()
    kept: list[dict[str, Any]] = []
    removed = 0
    for phrase in data.get("phrases", []):
        if str(phrase.get("text", "")).strip().lower() == target:
            for take in phrase.get("takes", []):
                name = take.get("file")
                if name:
                    try:
                        (path.parent / name).unlink(missing_ok=True)
                    except OSError as exc:
                        print(f"Warning: could not delete {name}: {exc}", file=sys.stderr)
            removed += 1
        else:
            kept.append(phrase)
    data["phrases"] = kept
    save_manifest(path, data)
    return removed


def record_take(seconds: float, sample_rate: int, vad_threshold: float) -> Any:
    samples, rate = record_audio(seconds, sample_rate, None, None)
    vad = trim_silence(samples, rate, vad_threshold)
    if not vad.has_speech:
        raise AudioError(vad.reason or "No speech was detected in the recording.")
    return mfcc(vad.trimmed, rate), vad


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record user voice templates for the closed-vocabulary recognizer.")
    parser.add_argument("--phrase", action="append", default=[], help="Additional phrase to record (repeatable).")
    parser.add_argument("--no-defaults", action="store_true", help="Record only phrases given with --phrase.")
    parser.add_argument("--takes", type=int, default=2, help="Takes per phrase (default 2).")
    parser.add_argument("--seconds", type=float, default=3.5, help="Maximum seconds per take (default 3.5).")
    parser.add_argument("--devices", action="store_true", help="Show microphone/speaker counts and names, then exit.")
    parser.add_argument("--list", action="store_true", help="List installed templates, then exit.")
    parser.add_argument("--remove", help="Remove all takes for an exact phrase, then exit.")
    parser.add_argument("--templates-dir", help="Override the configured template directory.")
    parser.add_argument("--yes", action="store_true", help="Skip the microphone-consent confirmation.")
    args = parser.parse_args(argv)

    config = Config.load(root=ROOT)
    templates_dir = Path(args.templates_dir) if args.templates_dir else config.path("speech.templates_dir")
    manifest_path = templates_dir / "manifest.json"
    manifest = load_manifest(manifest_path)

    if args.devices:
        status = audio_status()
        print(f"Microphones: {len(status.get('inputs', []))}")
        for device in status.get("inputs", []):
            print(f"  input {device.get('index')}: {device.get('name')}")
        print(f"Speakers: {len(status.get('outputs', []))}")
        for device in status.get("outputs", []):
            print(f"  output {device.get('index')}: {device.get('name')}")
        return 0

    if args.list:
        show_manifest(manifest_path, manifest)
        return 0

    if args.remove:
        removed = remove_phrase(manifest_path, manifest, args.remove)
        print(f"Removed {removed} phrase(s) matching {args.remove!r}.")
        return 0

    phrases = list(dict.fromkeys(([] if args.no_defaults else DEFAULT_PHRASES) + args.phrase))
    if not phrases:
        print("No phrases to record. Pass --phrase or omit --no-defaults.", file=sys.stderr)
        return 2
    if args.takes < 1 or args.takes > 10:
        print("--takes must be between 1 and 10.", file=sys.stderr)
        return 2
    if not 0.5 <= args.seconds <= 15:
        print("--seconds must be between 0.5 and 15.", file=sys.stderr)
        return 2

    status = audio_status()
    if not status.get("input_available"):
        print("No microphone is available. Voice templates were not recorded.", file=sys.stderr)
        return 2
    print(f"Microphone(s): {len(status.get('inputs', []))}")
    print(
        "This will record from the default microphone when you press Enter for each take. "
        "Audio is processed locally; only MFCC features are saved."
    )
    if not args.yes:
        answer = input("Proceed? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Cancelled. Nothing was recorded.")
            return 1

    sample_rate = int(config.get("speech.sample_rate", 16000))
    vad_threshold = float(config.get("speech.vad_energy_threshold", 0.008))
    manifest["sample_rate"] = sample_rate
    by_text = {str(item.get("text", "")).lower(): item for item in manifest["phrases"]}

    for phrase in phrases:
        phrase = " ".join(phrase.split())
        if not phrase:
            continue
        print(f"\nPhrase: {phrase!r}")
        entry = by_text.get(phrase.lower())
        if entry is None:
            entry = {"text": phrase, "intent": "", "takes": []}
            manifest["phrases"].append(entry)
            by_text[phrase.lower()] = entry
        for take_index in range(1, args.takes + 1):
            input(f"  Take {take_index}/{args.takes}: press Enter, then speak...")
            try:
                features, vad = record_take(args.seconds, sample_rate, vad_threshold)
            except AudioError as exc:
                print(f"  Recording failed: {exc}", file=sys.stderr)
                continue
            except Exception as exc:
                print(f"  Unexpected recording error: {exc.__class__.__name__}: {exc}", file=sys.stderr)
                continue
            stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
            digest = hashlib.sha256((phrase + stamp + str(take_index)).encode("utf-8")).hexdigest()[:10]
            filename = f"take_{digest}.npy"
            target = templates_dir / filename
            templates_dir.mkdir(parents=True, exist_ok=True)
            import numpy as np

            np.save(target, features, allow_pickle=False)
            checksum = hashlib.sha256(target.read_bytes()).hexdigest()
            entry["takes"] = [take for take in entry.get("takes", []) if take.get("file") != filename]
            entry["takes"].append(
                {
                    "file": filename,
                    "sha256": checksum,
                    "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "seconds": round(float(vad.duration_seconds), 3),
                    "sample_rate": sample_rate,
                }
            )
            manifest["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            save_manifest(manifest_path, manifest)
            print(f"  Saved {filename} ({take_index}/{args.takes}).")

    show_manifest(manifest_path, manifest)
    print(
        "\nVoice input now recognizes only these recorded phrases. "
        "Re-record if recognition is poor. No audio was uploaded."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
