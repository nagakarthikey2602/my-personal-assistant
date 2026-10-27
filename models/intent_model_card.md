# Local intent model card

- Trained at: 2026-10-27T11:32:41Z
- Algorithm: multinomial logistic regression (softmax), full-batch Adam, L2
- Pretrained weights: no
- Training examples: 518
- Labels: 52
- Features: 2688
- Training accuracy: 0.9826
- Validation accuracy: 0.872881
- User-approved examples included: 0
- Dataset provenance: Examples generated from templates in this repository and/or user-approved examples.

The model is a closed-label intent classifier trained only on locally authored or
user-approved examples. It can suggest an intent label. It never provides tool
arguments, grants approval, or bypasses the tool registry and permission policy.

Accuracy on this synthetic held-out split is not evidence of general language
understanding. The deterministic rule parser remains the primary command path.
