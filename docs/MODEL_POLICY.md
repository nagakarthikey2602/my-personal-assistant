# Local model and data policy

This project follows a simple rule: **the assistant can run without a cloud AI
service, an API key, telemetry, or a downloaded pretrained assistant model.**
The default runtime contains only deterministic code, small locally trained
components, and user-supplied recordings.

This is a policy for this repository and a description of what the code
actually does. It is not a legal opinion and does not certify third-party
components.

## What is used

| Component | Kind | Provenance | Network needed? |
|---|---|---|---|
| Deterministic parser/planner | Rules | Authored in this repository | No |
| Intent classifier | Multinomial logistic regression (softmax), NumPy | Trained from scratch on templates in this repository | No |
| Speech recognition | MFCC + dynamic time warping template matcher | User-recorded phrases; no acoustic model weights | No |
| Speech output | Deterministic formant synthesizer | Rule tables and DSP code in this repository | No |
| Screenshots, PDF/DOCX support, process/status queries | Ordinary local libraries/APIs | NumPy, Pillow, pypdf, psutil, Windows APIs | No |
| Optional SAPI voice | Microsoft-pretrained OS voices | Installed Windows SAPI component, invoked only by explicit opt-in | Treated as outside the local-model default |

There is no remote server, cloud API, telemetry endpoint, model download,
automatic update, or fallback to a hosted service in the runtime. `setup.ps1`
may use pip to install the declared Python packages; it is the only supplied
network-capable step.

## Intent classifier

The classifier is implemented in
`app/personal_assistant/intelligence/intent_model.py` as a small bag-of-words,
single-hidden-layer-free softmax regression. It is trained with full-batch Adam
and L2 regularization using NumPy. There are no pretrained weights, no
transformer, no vocabulary imported from a language model, and no external
inference service.

It is a **fallback label suggester**, not the command parser:

- the deterministic parser runs first;
- the model is consulted only when the parser marks the utterance unknown;
- a confidence and margin gate must pass (`0.55` and `0.15` by default);
- the label only selects a parser handler;
- arguments are still derived from deterministic text rules;
- the model cannot grant approval, invent arguments, call a tool, or bypass the
  registry, path policy, or offline guard;
- low-confidence or ambiguous input leads to clarification.

The recorded model was trained on 518 authored examples (52 labels, 2,688
features). On the synthetic held-out test split of 129 examples it achieved
**94.57% raw accuracy (122/129)**. Validation accuracy was **87.29%** on 118
examples. With the strict confidence/margin gates, only **20.16%** of test
examples were accepted, at **100% accepted accuracy**. The test split is
authored-template expansion, not natural human language; the raw score is not a
measure of general understanding or real-world task success. The low gate
coverage is intentional: the system prefers asking to guessing.

These numbers are recorded in `models/intent_eval.json`,
`models/intent_metrics.json`, and `models/intent_model_card.md`. Re-run the
reproducible pipeline with:

```powershell
.\.venv\Scripts\python.exe .\scripts\generate_training_data.py
.\.venv\Scripts\python.exe .\scripts\train_intent_model.py
.\.venv\Scripts\python.exe .\scripts\evaluate_intent_model.py
```

The generator is deterministic and writes train/validation/test splits plus a
manifest. Training does not read the test split. The optional
`--include-user-examples` flag adds only examples marked `approved=1` in the
local memory database; it is never enabled implicitly. The recorded model
included zero user examples. Approved examples remain local and are not uploaded.

## Dataset provenance

The default training data is synthetic text generated from templates authored in
`scripts/generate_training_data.py` and reviewed intent labels. It contains no
scraped web corpus, no third-party conversation data, and no pretrained
embeddings. If a user adds examples, they are stored in SQLite with source and
approval metadata. A future model card must report the number and provenance of
such examples; current tools do not automatically retrain after adding one.

## Speech components

- **ASR:** the recognizer compares MFCC sequences against templates recorded by
  the owner. It recognizes only recorded phrases. It is not general dictation,
  does not use Whisper or any pretrained acoustic model, and does not send audio
  anywhere.
- **TTS:** the default formant synthesizer is deterministic and has no learned
  weights. It sounds robotic and has not been perceptually validated.
- **Wake word:** not implemented. There is no always-listening mode. The
  `wake_word_enabled` setting is reserved and the service reports it as not
  implemented.

## The SAPI exception

Windows ships Microsoft-pretrained SAPI voices. They are **not** part of the
from-scratch default and are disabled by policy. To use one, the owner must
change both settings:

```yaml
speech:
  tts_engine: sapi
  allow_os_pretrained_tts: true
```

`Config.validate()` rejects `tts_engine: sapi` without the acknowledgement.
When enabled:

- speech is synthesized by the installed Windows SAPI component through a local
  PowerShell helper;
- the pretrained voice is a Microsoft OS component, not a model shipped or
  trained by this project;
- no automatic fallback from formant to SAPI exists;
- the model policy exception is visible in configuration and should be recorded
  in any deployment notes;
- network behavior of the OS component is outside the project's Python guard;
  the helper itself does not download voices.

If the project's requirement is interpreted as "no pretrained model of any
kind," leave `allow_os_pretrained_tts: false` and use the robotic formant voice.
The default configuration already does this.

## Prohibited and review-required additions

Do not add the following without an explicit project decision, a model card, and
a security review:

- cloud inference, API keys, telemetry, crash reporting, or remote logging;
- automatic model downloads, browser downloads, or update checks at runtime;
- a pretrained LLM, speech model, embedding model, or vision model as a hidden
  fallback;
- a model that can emit tool calls or arguments directly, bypassing the parser
  and policy;
- training on scraped personal data or third-party data without a documented
  basis;
- silent use of OS-pretrained voices or other pretrained components.

Any future local model should remain optional, be loaded from a local path, have
a model card with provenance and evaluation, fail closed when absent, and never
expand its own authority. The deterministic parser and tool registry remain the
authorization path.

## Reproducibility and integrity

The model and evaluation files are local JSON. The recorded SHA-256 values at
the time of acceptance are listed in
[INSTALLED_COMPONENTS.md](INSTALLED_COMPONENTS.md). There is no signature or
remote attestation; a local user can replace the model. The assistant does not
verify a signature before loading it. Treat the model directory as internal
runtime data (it is in the default protected roots) and protect it with Windows
account permissions.

## Disabling local intelligence

Set:

```yaml
intelligence:
  enable_model: false
```

or set `speech.asr_engine: none` and `speech.tts_engine: none` to disable those
subsystems. The deterministic parser and non-AI tools continue to work. A
missing or unreadable model is reported honestly; the assistant does not
download a replacement.
