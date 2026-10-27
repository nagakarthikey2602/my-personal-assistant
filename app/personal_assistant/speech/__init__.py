"""Offline speech subsystem.

No speech model is downloaded. The default ASR backend is a closed-vocabulary
template recognizer trained from the user's own recordings. The default TTS is a
deterministic formant synthesizer with no learned weights. OS voices are an
explicit opt-in because they are pretrained by Microsoft.
"""

from .template_asr import TemplateASR
from .tts import SpeechOutput, FormantSynthesizer

__all__ = ["TemplateASR", "SpeechOutput", "FormantSynthesizer"]
