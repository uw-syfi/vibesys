"""LLM-serving domain definition."""

from __future__ import annotations

from vibesys.constants import DomainName
from vibesys.domains.base import DomainDefinition
from vibesys.domains.llm_serving.hooks import LLMServingEnvironmentHooks
from vibesys.prompts import PROMPTS_DIR

DEFINITION = DomainDefinition(
    name=DomainName.LLM_SERVING,
    prompt_dir=PROMPTS_DIR / "domains" / "llm_serving",
    environment_hooks=LLMServingEnvironmentHooks(),
    supports_torch_profiler=True,
)
