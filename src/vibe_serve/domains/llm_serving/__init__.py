"""LLM-serving domain definition."""

from __future__ import annotations

from pathlib import Path

from vibe_serve.domains.base import DomainDefinition, DomainName
from vibe_serve.domains.llm_serving.hooks import LLMServingEnvironmentHooks

DEFINITION = DomainDefinition(
    name=DomainName.LLM_SERVING,
    prompt_dir=Path(__file__).resolve().parent / "templates",
    environment_hooks=LLMServingEnvironmentHooks(),
)
