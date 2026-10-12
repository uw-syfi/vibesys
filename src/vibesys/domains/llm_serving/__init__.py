"""LLM-serving domain definition."""

from __future__ import annotations

from pathlib import Path

from vibesys.constants import DomainName
from vibesys.domains.base import DomainDefinition

DEFINITION = DomainDefinition(
    name=DomainName.LLM_SERVING,
    prompt_dir=Path(__file__).resolve().parent / "prompts",
    supports_torch_profiler=True,
    modalities=(
        "text_generation",
        "image_generation",
        "video_generation",
        "text_to_speech",
        "speech_to_text",
        "realtime_audio",
    ),
    default_modality="text_generation",
)
