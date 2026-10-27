"""Train the local from-scratch intent classifier and evaluate it honestly.

No downloaded weights, cloud service, or pretrained model is used. The only
inputs are the authored generated dataset and, optionally, user-approved
examples stored in the local SQLite memory database.

Run from the repository root:

    .venv\\Scripts\\python.exe scripts\\train_intent_model.py
    .venv\\Scripts\\python.exe scripts\\train_intent_model.py --include-user-examples

The validation split is used for reporting only. The test split is never read
here; use scripts/evaluate_intent_model.py on it once, after training.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

# Small dense NumPy training under Windows can exhaust OpenBLAS worker buffers
# in constrained/background processes. One thread is faster and more reliable
# for this tiny model. An explicit user setting is respected.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from personal_assistant.config import Config  # noqa: E402
from personal_assistant.core.parser import CommandParser  # noqa: E402
from personal_assistant.intelligence.intent_model import (  # noqa: E402
    evaluate_model,
    save_model,
    train_softmax_model,
)
from personal_assistant.util import atomic_write_text  # noqa: E402


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path.name}:{line_number}: invalid JSON: {exc}") from exc
        if not isinstance(row.get("text"), str) or not isinstance(row.get("label"), str):
            raise SystemExit(f"{path.name}:{line_number}: needs text and label strings")
        rows.append(row)
    return rows


def approved_user_examples(config: Config) -> list[dict[str, str]]:
    database = Path(config.get("paths.database"))
    if not database.exists():
        return []
    try:
        with sqlite3.connect(str(database)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT text, label FROM training_examples WHERE approved=1 ORDER BY created_at"
            ).fetchall()
    except sqlite3.Error as exc:
        print(f"Warning: could not read approved user examples: {exc}", file=sys.stderr)
        return []
    return [{"text": str(row["text"]), "label": str(row["label"]), "source": "user_approved"} for row in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train the local softmax intent classifier.")
    parser.add_argument("--training-dir", help="Directory produced by generate_training_data.py.")
    parser.add_argument("--model", help="Output checkpoint path. Defaults to <APP_ROOT>/models/intent_model.json.")
    parser.add_argument("--metrics", help="Metrics output path. Defaults beside the model.")
    parser.add_argument("--max-features", type=int, default=3000)
    parser.add_argument("--epochs", type=int, default=600)
    parser.add_argument("--learning-rate", type=float, default=0.01)
    parser.add_argument("--l2", type=float, default=0.0005)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--include-user-examples",
        action="store_true",
        help="Add only approved local training examples from the memory database.",
    )
    args = parser.parse_args(argv)

    config = Config.load(root=ROOT)
    data_dir = Path(args.training_dir) if args.training_dir else config.path("paths.training_data") / "generated"
    model_path = Path(args.model) if args.model else config.path("intelligence.intent_model_path")
    metrics_path = Path(args.metrics) if args.metrics else model_path.with_name("intent_metrics.json")

    train_rows = read_jsonl(data_dir / "train.jsonl")
    validation_path = data_dir / "validation.jsonl"
    validation_rows = read_jsonl(validation_path) if validation_path.exists() else []

    allowed_labels = set(CommandParser.ACTION_INTENTS)
    bad = sorted({row["label"] for row in train_rows if row["label"] not in allowed_labels})
    if bad:
        print("Training data contains labels the parser cannot execute: " + ", ".join(bad), file=sys.stderr)
        return 2

    extra_rows: list[dict[str, Any]] = []
    if args.include_user_examples:
        extra_rows = approved_user_examples(config)
        invalid = sorted({row["label"] for row in extra_rows if row["label"] not in allowed_labels})
        extra_rows = [row for row in extra_rows if row["label"] in allowed_labels]
        if invalid:
            print("Ignored user examples with unsupported labels: " + ", ".join(invalid), file=sys.stderr)

    # Deduplicate while retaining deterministic order.
    seen: set[tuple[str, str]] = set()
    texts: list[str] = []
    labels: list[str] = []
    for row in train_rows + extra_rows:
        key = (row["text"].strip().lower(), row["label"])
        if key in seen:
            continue
        seen.add(key)
        texts.append(row["text"].strip())
        labels.append(row["label"])

    if len(set(labels)) < 2:
        print("Not enough distinct labels to train.", file=sys.stderr)
        return 2

    print(
        f"Training on {len(texts)} examples across {len(set(labels))} labels "
        f"({len(extra_rows)} user-approved additions)."
    )
    started = time.monotonic()
    model, metadata = train_softmax_model(
        texts,
        labels,
        max_features=args.max_features,
        epochs=args.epochs,
        learning_rate=args.learning_rate,
        l2=args.l2,
        seed=args.seed,
    )
    metadata["training_seconds"] = round(time.monotonic() - started, 3)
    metadata["dataset"] = str(data_dir)
    metadata["user_examples_included"] = len(extra_rows)
    metadata["label_distribution"] = dict(sorted(Counter(labels).items()))
    model.metadata = metadata

    validation: dict[str, Any] = {"examples": 0, "accuracy": None}
    if validation_rows:
        validation = evaluate_model(
            model,
            [row["text"] for row in validation_rows],
            [row["label"] for row in validation_rows],
        )
    metadata["validation_accuracy"] = validation.get("accuracy")
    metadata["validation_examples"] = validation.get("examples", 0)

    save_model(model, model_path)
    metrics = {
        "trained_at": metadata["trained_at"],
        "model_path": str(model_path),
        "dataset": str(data_dir),
        "training": {
            "examples": len(texts),
            "labels": len(model.labels),
            "features": len(model.vocabulary),
            "epochs_run": metadata["epochs_run"],
            "final_loss": metadata["final_loss"],
            "train_accuracy": metadata["train_accuracy"],
            "training_seconds": metadata["training_seconds"],
            "user_examples_included": len(extra_rows),
        },
        "validation": validation,
        "policy": "Local, from-scratch classifier. Label suggestion only; no action execution.",
    }
    atomic_write_text(metrics_path, json.dumps(metrics, ensure_ascii=False, indent=2))
    atomic_write_text(
        metrics_path.with_name("intent_model_card.md"),
        "\n".join(
            [
                "# Local intent model card",
                "",
                f"- Trained at: {metadata['trained_at']}",
                f"- Algorithm: {metadata['algorithm']}",
                f"- Pretrained weights: no",
                f"- Training examples: {len(texts)}",
                f"- Labels: {len(model.labels)}",
                f"- Features: {len(model.vocabulary)}",
                f"- Training accuracy: {metadata['train_accuracy']:.4f}",
                f"- Validation accuracy: {validation.get('accuracy')}",
                f"- User-approved examples included: {len(extra_rows)}",
                f"- Dataset provenance: {metadata['provenance']}",
                "",
                "The model is a closed-label intent classifier trained only on locally authored or",
                "user-approved examples. It can suggest an intent label. It never provides tool",
                "arguments, grants approval, or bypasses the tool registry and permission policy.",
                "",
                "Accuracy on this synthetic held-out split is not evidence of general language",
                "understanding. The deterministic rule parser remains the primary command path.",
            ]
        )
        + "\n",
    )

    print(f"Saved model: {model_path}")
    print(f"Train accuracy: {metadata['train_accuracy']:.3f}")
    if validation_rows:
        print(f"Validation accuracy: {validation['accuracy']:.3f} on {validation['examples']} examples")
        weakest = sorted(
            ((label, info["recall"]) for label, info in validation.get("per_class", {}).items()),
            key=lambda item: item[1],
        )[:5]
        print("Weakest validation labels: " + ", ".join(f"{label}={recall:.2f}" for label, recall in weakest))
    print(f"Metrics: {metrics_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
