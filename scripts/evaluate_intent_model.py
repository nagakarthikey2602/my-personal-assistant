"""Evaluate the trained local intent model on the held-out test split.

This script does not retrain or alter the model. It reports:

- raw top-1 accuracy;
- accuracy and coverage after the configured confidence/margin gates;
- per-label recall;
- confusion counts and the largest errors.

Synthetic held-out data measures consistency with the authored templates, not
general language understanding. The report says so explicitly.

Run from the repository root:

    .venv\\Scripts\\python.exe scripts\\evaluate_intent_model.py
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from personal_assistant.config import Config  # noqa: E402
from personal_assistant.intelligence.intent_model import IntentModel, evaluate_model  # noqa: E402
from personal_assistant.util import atomic_write_text  # noqa: E402


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path.name}:{line_number}: invalid JSON: {exc}") from exc
        rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the local intent model on held-out data.")
    parser.add_argument("--training-dir", help="Directory containing test.jsonl.")
    parser.add_argument("--model", help="Model checkpoint path.")
    parser.add_argument("--output", help="Optional JSON report path.")
    parser.add_argument("--confidence", type=float, help="Override the configured confidence gate.")
    parser.add_argument("--margin", type=float, help="Override the configured margin gate.")
    args = parser.parse_args(argv)

    config = Config.load(root=ROOT)
    data_dir = Path(args.training_dir) if args.training_dir else config.path("paths.training_data") / "generated"
    model_path = Path(args.model) if args.model else config.path("intelligence.intent_model_path")
    test_path = data_dir / "test.jsonl"
    if not test_path.exists():
        print(f"Missing {test_path}. Run scripts/generate_training_data.py first.", file=sys.stderr)
        return 2
    if not model_path.exists():
        print(f"Missing {model_path}. Run scripts/train_intent_model.py first.", file=sys.stderr)
        return 2

    rows = read_jsonl(test_path)
    if not rows:
        print("The test split is empty.", file=sys.stderr)
        return 2
    model = IntentModel.load(model_path)
    texts = [str(row["text"]) for row in rows]
    labels = [str(row["label"]) for row in rows]
    base = evaluate_model(model, texts, labels)

    confidence_gate = (
        float(args.confidence) if args.confidence is not None
        else float(config.get("intelligence.min_confidence", 0.55))
    )
    margin_gate = (
        float(args.margin) if args.margin is not None
        else float(config.get("intelligence.min_margin", 0.15))
    )
    predictions = [model.predict(text) for text in texts]
    accepted = [
        (prediction, label)
        for prediction, label in zip(predictions, labels)
        if prediction.confidence >= confidence_gate and prediction.margin >= margin_gate
    ]
    accepted_correct = sum(1 for prediction, label in accepted if prediction.label == label)
    accepted_accuracy = round(accepted_correct / len(accepted), 6) if accepted else None

    errors = Counter(
        (label, prediction.label)
        for prediction, label in zip(predictions, labels)
        if prediction.label != label
    )
    largest_errors = [
        {"expected": expected, "predicted": predicted, "count": count}
        for (expected, predicted), count in errors.most_common(20)
    ]
    report = {
        "model_path": str(model_path),
        "test_set": str(test_path),
        "dataset_provenance": "Authored templates and deterministic slot expansion; synthetic held-out split.",
        "caveat": (
            "This score measures consistency with authored templates. It is not a measure of "
            "general language understanding or real-world task accuracy."
        ),
        "raw": base,
        "gates": {
            "confidence": confidence_gate,
            "margin": margin_gate,
            "accepted": len(accepted),
            "coverage": round(len(accepted) / len(texts), 6),
            "accepted_accuracy": accepted_accuracy,
        },
        "largest_errors": largest_errors,
        "metadata": model.metadata,
    }

    print(f"Model: {model_path}")
    print(f"Test examples: {len(texts)}")
    print(f"Raw top-1 accuracy: {base['accuracy']:.3f} ({base['correct']}/{base['examples']})")
    print(
        f"After gates: coverage {report['gates']['coverage']:.1%}, "
        f"accepted accuracy {accepted_accuracy if accepted_accuracy is not None else 'n/a'}"
    )
    print("Per-label recall:")
    for label, info in sorted(base["per_class"].items()):
        print(f"  {label:32s} {info['recall']:.2f}  ({info['correct']}/{info['support']})")
    if largest_errors:
        print("Most common errors:")
        for item in largest_errors[:10]:
            print(f"  {item['expected']} -> {item['predicted']} ({item['count']})")

    if args.output:
        atomic_write_text(Path(args.output), json.dumps(report, ensure_ascii=False, indent=2))
        print(f"Wrote report: {args.output}")
    return 0 if base["accuracy"] >= 0.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
