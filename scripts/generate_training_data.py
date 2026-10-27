"""Generate the authored intent-classifier dataset.

The sentences in this file are written for this project. They are not copied
from a pretrained model or an external corpus. Slots are expanded locally, and
whole base sentences (not individual variants) are split into train/validation/
test groups so the same sentence cannot appear in more than one split.

Run from the repository root:

    .venv\\Scripts\\python.exe scripts\\generate_training_data.py

Outputs (by default, under <APP_ROOT>/training_data/generated):
    train.jsonl, validation.jsonl, test.jsonl, manifest.json

The test split is never used by train_intent_model.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from personal_assistant.config import Config  # noqa: E402
from personal_assistant.core.parser import CommandParser  # noqa: E402
from personal_assistant.util import atomic_write_text  # noqa: E402

SLOTS: dict[str, list[str]] = {
    "app": ["notepad", "calculator", "paint", "explorer"],
    "folder": ["reports", "receipts", "archive", "sandbox\\reports"],
    "file": ["notes.txt", "report.pdf", "budget.csv", "sandbox\\notes.txt"],
    "dest": ["sandbox\\archive", "downloads", "documents"],
    "newname": ["memo.txt", "final-report.txt", "renamed.dat"],
    "content": ["Hello world", "meeting notes", "alpha beta gamma", "draft text"],
    "query": ["budget", "invoice", "report"],
    "url": ["https://example.com", "https://example.org/docs", "http://localhost:8000"],
    "key": ["editor", "browser", "project folder"],
    "value": ["notepad", "firefox", "sandbox"],
    "log": ["sandbox\\app.log", "sandbox\\errors.log", "sandbox\\build.log"],
    "text": ["hello world", "good morning", "testing one two"],
    "setting": ["display", "bluetooth", "network", "power", "sound"],
    "action": ["pause", "mute", "next track", "volume up"],
    "speech": ["hello there", "good morning", "the task is complete"],
}

# Each sentence is an authored command. Braces expand with SLOTS deterministically.
LABEL_SENTENCES: dict[str, list[str]] = {
    "fs.search": [
        "find {query} files in downloads",
        "search documents for {query}",
        "look for files named {query} in sandbox",
        "search sandbox for files containing {query}",
        "find pdf files in downloads",
    ],
    "fs.read": [
        "read {file}",
        "show me {file}",
        "open the file {file}",
        "display the contents of {file}",
        "read the document {file}",
    ],
    "fs.list": [
        "list files in downloads",
        "show the contents of sandbox",
        "list documents",
        "show me what is in the desktop folder",
        "list the folder sandbox",
    ],
    "fs.write": [
        "create a file {file} in sandbox with content {content}",
        "write {content} to sandbox\\draft.txt",
        "save {content} into {file}",
        "create a text file notes.txt in sandbox with content {content}",
        "make a file called draft.txt with content {content}",
    ],
    "fs.create_folder": [
        "create a folder {folder} in sandbox",
        "make a directory called {folder} in downloads",
        "create the folder {folder} in documents",
        "make a new folder named project in sandbox",
        "create a directory receipts in sandbox",
    ],
    "fs.copy": [
        "copy {file} to {dest}",
        "duplicate {file} into sandbox\\backup.txt",
        "copy notes.txt to sandbox\\archive",
        "make a copy of {file} in downloads",
        "copy the file report.pdf to documents",
    ],
    "fs.move": [
        "move {file} to {dest}",
        "transfer {file} into sandbox\\archive",
        "move report.pdf to documents",
        "move notes.txt into the archive folder",
        "move the file budget.csv to downloads",
    ],
    "fs.rename": [
        "rename {file} to {newname}",
        "rename notes.txt to memo.txt",
        "rename the file report.pdf as final-report.pdf",
        "rename sandbox\\draft.txt to final.txt",
        "rename the folder receipts to archive",
    ],
    "fs.quarantine_delete": [
        "delete {file}",
        "remove the file {file}",
        "quarantine {file}",
        "trash sandbox\\draft.txt",
        "delete the folder sandbox\\archive",
    ],
    "fs.zip": [
        "zip sandbox\\reports to sandbox\\reports.zip",
        "compress {folder} into sandbox\\archive.zip",
        "archive documents to sandbox\\documents.zip",
        "zip the folder reports",
        "create a zip of sandbox\\reports",
    ],
    "fs.extract": [
        "extract sandbox\\reports.zip to sandbox\\out",
        "unzip sandbox\\archive.zip into sandbox\\archive-files",
        "extract the archive sandbox\\documents.zip to sandbox\\docs",
        "unzip downloads\\bundle.zip to sandbox\\bundle",
        "extract sandbox\\reports.zip",
    ],
    "fs.duplicates": [
        "find duplicate files in downloads",
        "search for duplicates in sandbox",
        "show duplicate files in documents",
        "find duplicates in sandbox",
        "look for duplicate files in desktop",
    ],
    "fs.inventory": [
        "inventory downloads",
        "show a file inventory of documents",
        "create an inventory of sandbox",
        "generate an inventory for the desktop folder",
        "inventory of sandbox",
    ],
    "fs.undo_last": [
        "undo",
        "undo the last action",
        "revert the previous change",
        "undo last operation",
        "revert the last action",
    ],
    "apps.launch": [
        "open {app}",
        "launch {app}",
        "start {app}",
        "open the {app} app",
        "run {app}",
    ],
    "apps.list_windows": [
        "list open windows",
        "what windows are open",
        "show windows",
        "list the open application windows",
        "what apps have open windows",
    ],
    "apps.focus_window": [
        "focus {app}",
        "switch to {app}",
        "bring {app} to the front",
        "focus the {app} window",
        "switch to the {app} window",
    ],
    "apps.close": [
        "close {app}",
        "quit {app}",
        "terminate {app}",
        "close the {app} app",
        "force close {app}",
    ],
    "apps.list_installed": [
        "list installed apps",
        "what applications are installed",
        "show installed programs",
        "list the installed applications",
        "which apps are installed",
    ],
    "apps.type_text": [
        "type {text}",
        "type {text} in notepad",
        "enter the text {text}",
        "type the text {text}",
        "write {text} using the keyboard",
    ],
    "apps.capabilities": [
        "app capabilities",
        "application capabilities",
        "what can you do with applications",
        "show app capabilities",
        "what app actions are available",
    ],
    "windows.display_info": [
        "display info",
        "screen resolution",
        "show monitor status",
        "display information",
        "what is my screen resolution",
    ],
    "windows.network_status": [
        "network status",
        "wifi status",
        "internet status",
        "show network status",
        "is the network connected",
    ],
    "windows.bluetooth_status": [
        "bluetooth status",
        "is bluetooth on",
        "bluetooth state",
        "show bluetooth status",
        "check bluetooth",
    ],
    "windows.power_status": [
        "power status",
        "power plan",
        "sleep settings",
        "show the power plan",
        "what is the power status",
    ],
    "windows.startup_list": [
        "startup apps",
        "list startup entries",
        "show startup applications",
        "show startup apps",
        "list startup programs",
    ],
    "windows.open_settings": [
        "open settings",
        "open display settings",
        "show bluetooth settings",
        "open {setting} settings",
        "launch windows settings",
    ],
    "windows.open_folder": [
        "open the downloads folder",
        "open documents folder",
        "show the desktop folder",
        "open the sandbox folder",
        "show the documents directory",
    ],
    "system.info": [
        "system info",
        "computer specs",
        "show system information",
        "what are my computer specifications",
        "give me a system report",
    ],
    "system.top_processes": [
        "top processes",
        "what is using memory",
        "highest cpu processes",
        "show the top processes",
        "what processes are using cpu",
    ],
    "system.media_control": [
        "media control {action}",
        "mute",
        "next track",
        "volume up",
        "pause",
    ],
    "system.battery": [
        "battery status",
        "battery level",
        "charge status",
        "show battery status",
        "how much battery is left",
    ],
    "system.audio_devices": [
        "audio devices",
        "sound status",
        "list audio devices",
        "show sound devices",
        "what audio devices are available",
    ],
    "system.offline_status": [
        "offline status",
        "are you offline",
        "network guard status",
        "show offline mode",
        "is offline mode active",
    ],
    "desktop.clipboard_get": [
        "read the clipboard",
        "what is on the clipboard",
        "show clipboard",
        "read clipboard contents",
        "tell me what is in the clipboard",
    ],
    "desktop.clipboard_set": [
        "copy hello to the clipboard",
        "put the text hello on the clipboard",
        "set the clipboard to hello",
        "copy this text to the clipboard",
        "write hello to the clipboard",
    ],
    "desktop.screenshot": [
        "take a screenshot",
        "capture the screen",
        "screenshot",
        "take a screenshot of the screen",
        "capture a screenshot",
    ],
    "dev.run": [
        "run the tests",
        "run pytest",
        "git status",
        "git log",
        "git diff",
        "run compileall",
    ],
    "dev.analyze_log": [
        "analyze the log {log}",
        "check the error log {log}",
        "inspect the build log {log}",
        "scan {log} for errors",
        "analyze log file {log}",
    ],
    "dev.status": [
        "dev status",
        "developer status",
        "show developer command status",
        "development status",
        "show dev status",
    ],
    "browser.open": [
        "open {url}",
        "go to {url}",
        "visit {url}",
        "open the url {url}",
        "browse to {url}",
    ],
    "memory.remember": [
        "remember that my {key} is {value}",
        "remember my {key} is {value}",
        "remember the {key} is {value}",
        "keep in mind that my {key} is {value}",
        "note that my {key} is {value}",
    ],
    "memory.list": [
        "what do you remember",
        "list my preferences",
        "show memory",
        "show my preferences",
        "what preferences do you have",
    ],
    "memory.forget": [
        "forget my {key}",
        "delete memory key {key}",
        "forget the preference {key}",
        "delete the preference {key}",
        "forget everything",
    ],
    "memory.status": [
        "memory status",
        "assistant memory status",
        "show memory status",
        "is memory enabled",
        "status of local memory",
    ],
    "memory.export": [
        "export memory to sandbox\\memory.json",
        "save my memory to sandbox\\mem.json",
        "export my memory to sandbox\\export.json",
        "write memory to sandbox\\memory-export.json",
        "save memory into sandbox\\backup.json",
    ],
    "memory.add_example": [
        "add training example \"open notepad\" as apps.launch",
        "remember a training example \"take a screenshot\" as desktop.screenshot",
        "add training example \"battery status\" as system.battery",
        "add a training example \"list files in downloads\" as fs.list",
        "remember training example \"undo\" as fs.undo_last",
    ],
    "memory.set_enabled": [
        "disable memory",
        "enable memory",
        "turn off memory",
        "turn on memory",
        "disable the memory",
    ],
    "voice.status": [
        "voice status",
        "speech status",
        "show voice status",
        "is voice input available",
        "show speech status",
    ],
    "voice.list_devices": [
        "voice devices",
        "list microphones and speakers",
        "list microphones",
        "show speech devices",
        "list the audio input and output devices",
    ],
    "voice.speak": [
        "speak {speech}",
        "say {speech}",
        "speak the text {speech}",
        "say the words {speech}",
        "read aloud {speech}",
    ],
    "voice.capabilities": [
        "voice capabilities",
        "speech capabilities",
        "show voice capabilities",
        "what speech features are available",
        "tell me the voice limitations",
    ],
}


def _expand(template: str, variant: int) -> str:
    text = template
    for name, values in SLOTS.items():
        token = "{" + name + "}"
        if token in text:
            # Offset each slot so one sentence gets a coherent mix, not the same index everywhere.
            text = text.replace(token, values[(variant + len(name)) % len(values)], 1)
    return text


def _split_for(group_key: str) -> str:
    bucket = int(hashlib.sha256(group_key.encode("utf-8")).hexdigest()[:8], 16) % 100
    if bucket < 70:
        return "train"
    if bucket < 85:
        return "validation"
    return "test"


def build_examples() -> list[dict[str, Any]]:
    import re

    examples: list[dict[str, Any]] = []
    for label, templates in LABEL_SENTENCES.items():
        for template_index, template in enumerate(templates):
            variants = 4 if "{" in template else 1
            for variant in range(variants):
                text = _expand(template, variant)
                text = re.sub(r"\s+", " ", text).strip()
                group = f"{label}|{template_index}|{variant}"
                examples.append(
                    {
                        "text": text,
                        "label": label,
                        "split": _split_for(group),
                        "group": f"{template_index}:{variant}",
                    }
                )
                # Light, authored politeness variants are separate groups, so they
                # cannot leak between train and evaluation.
                if variant in {0, 1} and not template.startswith(("undo", "revert", "git ", "run ")):
                    polite_group = group + "|polite"
                    polite = ("please " + text)[0].upper() + ("please " + text)[1:]
                    examples.append(
                        {
                            "text": polite,
                            "label": label,
                            "split": _split_for(polite_group),
                            "group": f"{template_index}:{variant}:politeness",
                        }
                    )
    return examples


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the authored intent training data.")
    parser.add_argument("--output-dir", help="Defaults to <APP_ROOT>/training_data/generated.")
    args = parser.parse_args(argv)

    config = Config.load(root=ROOT)
    output_dir = Path(args.output_dir) if args.output_dir else config.path("paths.training_data") / "generated"
    output_dir.mkdir(parents=True, exist_ok=True)

    covered = set(LABEL_SENTENCES)
    expected = set(CommandParser.ACTION_INTENTS)
    missing = sorted(expected - covered)
    extra = sorted(covered - expected)
    if missing or extra:
        print("Intent label mismatch.", file=sys.stderr)
        if missing:
            print("  Missing: " + ", ".join(missing), file=sys.stderr)
        if extra:
            print("  Not in parser: " + ", ".join(extra), file=sys.stderr)
        return 2

    examples = build_examples()
    # Deduplicate identical text+label pairs.
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for example in examples:
        key = (example["text"].lower(), example["label"])
        if key not in seen:
            seen.add(key)
            unique.append(example)

    by_split: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for example in unique:
        by_split[example["split"]].append(example)

    manifest: dict[str, Any] = {
        "provenance": "Authored sentences and deterministic local slot expansion in scripts/generate_training_data.py",
        "pretrained": False,
        "labels": sorted(covered),
        "slot_values": SLOTS,
        "total_unique": len(unique),
        "splits": {},
    }
    for split in ("train", "validation", "test"):
        rows = sorted(by_split[split], key=lambda item: (item["label"], item["text"]))
        payload = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + ("\n" if rows else "")
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        atomic_write_text(output_dir / ("validation.jsonl" if split == "validation" else f"{split}.jsonl"), payload)
        counts = Counter(row["label"] for row in rows)
        manifest["splits"][split] = {
            "examples": len(rows),
            "sha256": digest,
            "per_label": dict(sorted(counts.items())),
        }
    atomic_write_text(output_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    print(f"Wrote {len(unique)} unique examples to {output_dir}")
    for split, info in manifest["splits"].items():
        print(f"  {split}: {info['examples']} examples")
    if manifest["splits"]["test"]["examples"] == 0:
        print("Warning: the test split is empty; adjust split bucketing.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
