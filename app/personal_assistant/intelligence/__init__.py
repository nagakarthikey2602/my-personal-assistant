"""Local, replaceable intelligence components trained from user-approved data."""

from .intent_model import IntentModel, ModelPrediction, train_softmax_model

__all__ = ["IntentModel", "ModelPrediction", "train_softmax_model"]
