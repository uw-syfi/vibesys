"""LLM-serving domain definition."""

from __future__ import annotations

from pathlib import Path

from vibesys.domains.base import DomainDefinition, DomainName
from vibesys.domains.llm_serving.hooks import LLMServingEnvironmentHooks

DEFINITION = DomainDefinition(
    name=DomainName.LLM_SERVING,
    prompt_dir=Path(__file__).resolve().parent / "templates",
    environment_hooks=LLMServingEnvironmentHooks(),
    supports_torch_profiler=True,
)
