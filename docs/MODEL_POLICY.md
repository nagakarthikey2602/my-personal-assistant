# Local model and data policy

This project follows a simple rule: **the assistant can run without a cloud AI
service, an API key, telemetry, or a downloaded pretrained assistant model.**
The runtime contains deterministic code, small locally trained components, and
user-supplied recordings. Three pretrained components are explicit, opt-in
exceptions: the optional local Vosk wake/fallback model, the optional local
faster-whisper command model, and optional Windows SAPI voices. None is a cloud
service; none is a silent fallback.

This is a policy for this repository and a description of what the code
actually does. It is not a legal opinion and does not certify third-party
components.

## What is used

| Component | Kind | Provenance | Network needed? |
|---|---|---|---|
| Deterministic parser/planner | Rules | Authored in this repository | No |
| Intent classifier | Multinomial logistic regression (softmax), NumPy | Trained from scratch on templates in this repository | No |
| Template speech recognition | MFCC + dynamic time warping template matcher | User-recorded phrases; no acoustic model weights | No |
| Optional Vosk speech recognition | Pretrained Kaldi acoustic/language model, Apache-2.0 | Downloaded once from `alphacephei.com` by `scripts/install_voice_model.py`; loaded from `models/vosk` | One-time explicit download only |
| Optional faster-whisper command recognition | OpenAI Whisper `tiny.en` weights, MIT, via faster-whisper/CTranslate2 | Downloaded once from Hugging Face by `scripts/install_whisper_model.py`; loaded from `models/whisper` in a local worker | One-time explicit download only |
| Speech output | Deterministic formant synthesizer | Rule tables and DSP code in this repository | No |
| Screenshots, PDF/DOCX support, process/status queries | Ordinary local libraries/APIs | NumPy, Pillow, pypdf, psutil, Windows APIs | No |
| Optional SAPI voice | Microsoft-pretrained OS voices | Installed Windows SAPI component, invoked only by explicit opt-in | Treated as outside the local-model default |

There is no remote server, cloud API, telemetry endpoint, runtime model
download, automatic update, or fallback to a hosted service. `setup.ps1` may use
pip to install declared Python packages, and `scripts/install_voice_model.py`
may download the optional Vosk model when the owner runs it explicitly. These
are the only supplied network-capable steps.

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

- **Template ASR:** compares MFCC sequences against templates recorded by the
  owner. It recognizes only recorded phrases. It does not use pretrained
  acoustic weights and does not send audio anywhere.
- **Vosk ASR (optional):** uses Vosk 0.3.45 and the
  `vosk-model-small-en-us-0.15` Kaldi model, both Apache-2.0. The model is
  downloaded once by the owner through `scripts/install_voice_model.py`, stored
  under `models/vosk`, and loaded locally. It provides constrained wake-word
  detection and a fallback general-command recognizer. If the package or model is
  missing, the service reports that and fails closed; it never falls back to a
  cloud recognizer. See [INSTALLED_COMPONENTS.md](INSTALLED_COMPONENTS.md).
- **faster-whisper command ASR (optional):** uses faster-whisper 1.2.1,
  CTranslate2, and OpenAI's `tiny.en` Whisper weights (MIT), all local. The model
  is downloaded once by the owner through `scripts/install_whisper_model.py`,
  stored under `models/whisper`, and loaded with `local_files_only=True` in a
  persistent local child process. Audio crosses only a local pipe and is never
  written to disk or sent over a network. If it is unavailable, wake commands
  fall back to the local Vosk recognizer rather than to any cloud service.
- **TTS:** the default formant synthesizer is deterministic and has no learned
  weights. It sounds robotic and has not been perceptually validated.
- **Wake word:** opt-in. Constrained Vosk decoding detects the wake phrase; the
  default installed word is “hearty”. The small model usually spells it
  “hardy”, so conservative aliases are accepted. Listening starts only after the
  owner enables it in Settings, clicks `🎧 Wake`, or passes `--wake`. While it is
  active the microphone remains open, a status badge is visible, and
  `Wake: off` releases the device. There is no hidden always-on mode.

## The speech-model exception

General speech recognition is not from scratch. The owner must make explicit
choices:

1. run `scripts/install_voice_model.py` for local wake detection (and the Vosk
   fallback), which is the only Vosk model-download path;
2. optionally run `scripts/install_whisper_model.py` for more accurate local
   command recognition, which is the only Whisper model-download path;
3. set `speech.asr_engine` to `vosk` or `whisper` for push-to-talk/general use,
   and choose `speech.wake_command_engine` (`whisper` by default, `vosk` as the
   fallback).

The models are loaded from local directories and are never fetched by the
running assistant. `speech.wake_word_enabled: true` additionally opts into
continuous microphone capture while the listener is active; the example
configuration keeps it off. Audio is processed in memory and is not written to
disk by Vosk, Whisper, or the wake listener.

If a deployment treats even these explicit local models as unacceptable, use
`speech.asr_engine: template` for user-recorded phrases, or `none` to disable
speech input.

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
kind," leave `allow_os_pretrained_tts: false`, use the robotic formant voice,
and keep `speech.asr_engine` at `template` or `none`. The default configuration
example does this.

## Prohibited and review-required additions

Do not add the following without an explicit project decision, a model card, and
a security review:

- cloud inference, API keys, telemetry, crash reporting, or remote logging;
- automatic model downloads, browser downloads, or update checks at runtime;
- a pretrained LLM, embedding model, or vision model as a hidden fallback;
- a speech model other than the documented, explicit local Vosk/Whisper
  exceptions;
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

The intent model and evaluation files are local JSON. Recorded SHA-256 values at
the time of acceptance are listed in [INSTALLED_COMPONENTS.md](INSTALLED_COMPONENTS.md).
The optional Vosk model is a third-party directory installed by
`scripts/install_voice_model.py`; that script prints the archive SHA-256 and
refuses unsafe archive paths. The optional Whisper model is installed by
`scripts/install_whisper_model.py` from a pinned repository name into
`models/whisper` and is never fetched at runtime. There is no signature or remote
attestation; a local user can replace the intent model or either speech model.
The assistant does not verify a signature before loading them. Treat the model
directory as internal runtime data (it is in the default protected roots) and
protect it with Windows account permissions.

## Disabling local intelligence

Set:

```yaml
intelligence:
  enable_model: false
```

or set `speech.asr_engine: none` and `speech.tts_engine: none` to disable those
subsystems. Disable only hands-free listening with
`speech.wake_word_enabled: false` and `Wake: off`; to remove the Whisper command
path while keeping wake detection, set `speech.wake_command_engine: vosk` or
delete the local Whisper model. The deterministic parser and non-AI tools
continue to work. A missing or unreadable model is reported honestly; the
assistant does not download a replacement.
