"""Speech status, device listing, and explicit local speech output."""

from __future__ import annotations

from typing import Any

from ..core.context import ToolContext
from ..core.registry import RiskLevel, ToolSpec, object_schema
from ..core.results import ToolResult


def register(registry) -> None:  # type: ignore[no-untyped-def]
    registry.register(ToolSpec(
        name="voice.status",
        description="Report microphone/speaker devices, ASR template availability, and TTS backend.",
        risk=RiskLevel.READ_ONLY,
        category="voice",
        schema=object_schema({}),
        handler=_status,
    ))
    registry.register(ToolSpec(
        name="voice.list_devices",
        description="List Windows microphone and speaker devices.",
        risk=RiskLevel.READ_ONLY,
        category="voice",
        schema=object_schema({}),
        handler=_devices,
    ))
    registry.register(ToolSpec(
        name="voice.speak",
        description="Speak text locally using the configured offline TTS backend.",
        risk=RiskLevel.REVERSIBLE,
        category="voice",
        schema=object_schema({
            "text": {"type": "string", "minLength": 1, "maxLength": 2000},
        }, required=["text"]),
        handler=_speak,
    ))
    registry.register(ToolSpec(
        name="voice.capabilities",
        description="Explain what the offline voice subsystem can and cannot do.",
        risk=RiskLevel.READ_ONLY,
        category="voice",
        schema=object_schema({}),
        handler=_capabilities,
    ))


def _status(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from ..speech.service import SpeechService

    service = SpeechService(ctx.config)
    status = service.status()
    available = bool(status["asr"].get("available")) and bool(status["audio"].get("input_available"))
    return ToolResult.success(
        "voice.status",
        f"Voice ASR available={status['asr'].get('available')}; "
        f"microphones={len(status['audio'].get('inputs', []))}; "
        f"TTS={status['tts'].get('configured_engine')}.",
        data=status,
        verified=True if not available else None,
    )


def _devices(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from ..speech.native_audio import audio_status

    status = audio_status()
    return ToolResult.success(
        "voice.list_devices",
        f"Found {len(status['inputs'])} microphone(s) and {len(status['outputs'])} speaker(s).",
        data=status,
    )


def _speak(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from ..speech.service import SpeechService

    ctx.check_cancel()
    service = SpeechService(ctx.config)
    if not service.tts.backend:
        return ToolResult.error("voice.speak", service.tts.error or "Speech output is unavailable.")
    try:
        service.speak(args["text"])
    except Exception as exc:
        return ToolResult.error("voice.speak", f"Local speech output failed: {exc.__class__.__name__}: {exc}")
    return ToolResult.unverified(
        "voice.speak",
        "Speech output was generated and sent to the audio device. Audibility cannot be verified automatically.",
        data={"engine": service.tts.status().get("backend"), "verification": "not available"},
    )


def _capabilities(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from ..speech.native_audio import audio_status

    return ToolResult.success(
        "voice.capabilities",
        "Push-to-talk voice input is implemented with a closed-vocabulary, user-trained recognizer. "
        "Arbitrary speech dictation and wake-word listening are not implemented.",
        data={
            "implemented": [
                "microphone/speaker discovery via WinMM",
                "push-to-talk capture",
                "energy VAD and recording quality checks",
                "user-recorded MFCC/DTW phrase recognition",
                "rule-based formant speech output",
                "optional policy-gated Windows SAPI voices",
            ],
            "not_implemented": [
                "general dictation",
                "always-listening wake word",
                "speaker diarization",
                "pretrained or downloaded speech models",
                "cloud STT/TTS",
            ],
            "devices": audio_status(),
            "policy": "AI models must be user-trained or from scratch; OS voices are explicit opt-in.",
        },
    )
