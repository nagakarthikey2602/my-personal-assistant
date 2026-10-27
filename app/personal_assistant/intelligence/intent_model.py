"""A tiny softmax intent classifier implemented from scratch with NumPy.

This is not a pretrained model. Training data is authored in this repository
(and may be extended with user-approved examples), and training runs locally.
The model can only choose an intent label; it never executes actions and cannot
bypass schemas, policy, or tool registration.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..errors import ToolUnavailable, ValidationError
from ..util import atomic_write_text
from .features import build_vocabulary, vectorize, vectorize_many, word_tokens

MODEL_TYPE = "pa-softmax-bow-v1"


@dataclass
class ModelPrediction:
    label: str
    confidence: float
    margin: float
    alternatives: list[dict[str, float]] = field(default_factory=list)


@dataclass
class IntentModel:
    labels: list[str]
    vocabulary: list[str]
    weights: np.ndarray
    bias: np.ndarray
    metadata: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None

    @classmethod
    def load(cls, path: str | Path) -> "IntentModel":
        model_path = Path(path)
        if not model_path.exists():
            raise ToolUnavailable("The local intent model has not been trained yet.")
        try:
            data = json.loads(model_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError("The intent model checkpoint is unreadable.") from exc
        if data.get("model_type") != MODEL_TYPE:
            raise ValidationError("The intent model type is not supported. Refusing to load it.")
        try:
            labels = [str(item) for item in data["labels"]]
            vocabulary = [str(item) for item in data["vocabulary"]]
            weights = np.asarray(data["weights"], dtype=np.float64)
            bias = np.asarray(data["bias"], dtype=np.float64)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("The intent model checkpoint is malformed.") from exc
        if weights.shape != (len(labels), len(vocabulary)) or bias.shape != (len(labels),):
            raise ValidationError("The intent model dimensions are inconsistent.")
        return cls(labels, vocabulary, weights, bias, dict(data.get("metadata", {})), model_path)

    def predict(self, text: str) -> ModelPrediction:
        if not text.strip():
            raise ValidationError("Cannot classify empty text.")
        vector = vectorize(text, self.vocabulary)
        scores = self.weights @ vector + self.bias
        scores -= np.max(scores)
        probabilities = np.exp(scores)
        probabilities /= max(1e-12, probabilities.sum())
        order = np.argsort(probabilities)[::-1]
        top = int(order[0])
        second = float(probabilities[int(order[1])]) if len(order) > 1 else 0.0
        alternatives = [
            {"label": self.labels[int(index)], "confidence": round(float(probabilities[int(index)]), 4)}
            for index in order[:5]
        ]
        return ModelPrediction(
            label=self.labels[top],
            confidence=float(probabilities[top]),
            margin=float(probabilities[top] - second),
            alternatives=alternatives,
        )

    def status(self) -> dict[str, Any]:
        return {
            "available": True,
            "type": MODEL_TYPE,
            "path": str(self.path) if self.path else "",
            "labels": len(self.labels),
            "features": len(self.vocabulary),
            "metadata": self.metadata,
            "policy": "Trained locally from authored/user-approved examples; no external pretrained weights.",
        }


def _softmax(scores: np.ndarray) -> np.ndarray:
    shifted = scores - np.max(scores, axis=1, keepdims=True)
    exp = np.exp(shifted)
    return exp / np.maximum(1e-12, exp.sum(axis=1, keepdims=True))


def train_softmax_model(
    texts: list[str],
    labels: list[str],
    *,
    max_features: int = 4000,
    epochs: int = 600,
    learning_rate: float = 0.01,
    l2: float = 0.0005,
    seed: int = 42,
) -> tuple[IntentModel, dict[str, Any]]:
    if not texts or len(texts) != len(labels):
        raise ValueError("Training needs non-empty texts and matching labels.")
    label_names = sorted(set(labels))
    if len(label_names) < 2:
        raise ValueError("At least two intent labels are required.")
    vocabulary = build_vocabulary(texts, max_features=max_features)
    X = vectorize_many(texts, vocabulary)
    label_index = {label: index for index, label in enumerate(label_names)}
    y = np.asarray([label_index[label] for label in labels], dtype=np.int64)
    Y = np.zeros((len(texts), len(label_names)), dtype=np.float64)
    Y[np.arange(len(texts)), y] = 1.0
    rng = np.random.default_rng(seed)
    weights = rng.normal(0.0, 0.01, size=(len(label_names), len(vocabulary)))
    bias = np.zeros(len(label_names), dtype=np.float64)
    best_weights = weights.copy()
    best_bias = bias.copy()
    best_loss = float("inf")
    # Adam is implemented here from scratch; it is an optimizer, not a
    # pretrained model. It makes the tiny full-batch problem converge reliably.
    beta1, beta2, epsilon = 0.9, 0.999, 1e-8
    moment_w = np.zeros_like(weights)
    velocity_w = np.zeros_like(weights)
    moment_b = np.zeros_like(bias)
    velocity_b = np.zeros_like(bias)
    loss_history: list[float] = []
    stagnant = 0
    for epoch in range(1, epochs + 1):
        scores = X @ weights.T + bias
        probabilities = _softmax(scores)
        loss = float(-np.mean(np.sum(Y * np.log(np.maximum(probabilities, 1e-12)), axis=1)))
        loss += float(l2 * np.sum(weights * weights))
        loss_history.append(loss)
        if loss < best_loss - 1e-7:
            best_loss = loss
            best_weights = weights.copy()
            best_bias = bias.copy()
            stagnant = 0
        else:
            stagnant += 1
        gradient_w = (probabilities - Y).T @ X / len(texts) + 2.0 * l2 * weights
        gradient_b = (probabilities - Y).sum(axis=0) / len(texts)
        moment_w = beta1 * moment_w + (1.0 - beta1) * gradient_w
        velocity_w = beta2 * velocity_w + (1.0 - beta2) * (gradient_w * gradient_w)
        moment_b = beta1 * moment_b + (1.0 - beta1) * gradient_b
        velocity_b = beta2 * velocity_b + (1.0 - beta2) * (gradient_b * gradient_b)
        correction1 = 1.0 - beta1 ** epoch
        correction2 = 1.0 - beta2 ** epoch
        weights -= learning_rate * (moment_w / correction1) / (np.sqrt(velocity_w / correction2) + epsilon)
        bias -= learning_rate * (moment_b / correction1) / (np.sqrt(velocity_b / correction2) + epsilon)
        if stagnant >= 120:
            break
    epochs_run = epoch
    train_predictions = np.argmax(X @ best_weights.T + best_bias, axis=1)
    train_accuracy = float(np.mean(train_predictions == y))
    data_hash = hashlib.sha256(("\n".join(texts) + "\n" + "\n".join(labels)).encode("utf-8")).hexdigest()
    metadata = {
        "model_type": MODEL_TYPE,
        "trained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "training_examples": len(texts),
        "labels": label_names,
        "features": len(vocabulary),
        "epochs_requested": epochs,
        "epochs_run": epochs_run,
        "final_loss": round(best_loss, 8),
        "train_accuracy": round(train_accuracy, 6),
        "data_sha256": data_hash,
        "algorithm": "multinomial logistic regression (softmax), full-batch Adam, L2",
        "pretrained": False,
        "provenance": "Examples generated from templates in this repository and/or user-approved examples.",
        "limitations": "Small closed-label intent classifier; not a general language model.",
    }
    model = IntentModel(label_names, vocabulary, best_weights, best_bias, metadata)
    return model, metadata


def save_model(model: IntentModel, path: str | Path) -> Path:
    destination = Path(path)
    payload = {
        "model_type": MODEL_TYPE,
        "version": 1,
        "labels": model.labels,
        "vocabulary": model.vocabulary,
        "weights": np.round(model.weights, 6).tolist(),
        "bias": np.round(model.bias, 6).tolist(),
        "metadata": model.metadata,
    }
    atomic_write_text(destination, json.dumps(payload, ensure_ascii=False))
    model.path = destination
    return destination


def evaluate_model(model: IntentModel, texts: list[str], labels: list[str]) -> dict[str, Any]:
    if not texts:
        return {"accuracy": 0.0, "examples": 0, "confusion": {}, "per_class": {}}
    predictions = [model.predict(text) for text in texts]
    correct = sum(1 for prediction, label in zip(predictions, labels) if prediction.label == label)
    confusion: dict[str, dict[str, int]] = {}
    per_class: dict[str, dict[str, float]] = {}
    for label in sorted(set(labels) | set(model.labels)):
        indices = [index for index, value in enumerate(labels) if value == label]
        if indices:
            class_correct = sum(1 for index in indices if predictions[index].label == label)
            per_class[label] = {
                "support": len(indices),
                "correct": class_correct,
                "recall": round(class_correct / len(indices), 4),
            }
    for prediction, label in zip(predictions, labels):
        confusion.setdefault(label, {})
        confusion[label][prediction.label] = confusion[label].get(prediction.label, 0) + 1
    mean_confidence = float(np.mean([prediction.confidence for prediction in predictions])) if predictions else 0.0
    return {
        "accuracy": round(correct / len(texts), 6),
        "examples": len(texts),
        "correct": correct,
        "mean_confidence": round(mean_confidence, 4),
        "confusion": confusion,
        "per_class": per_class,
    }
