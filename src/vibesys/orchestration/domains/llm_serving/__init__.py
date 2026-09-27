"""LLM-serving domain definition."""

from __future__ import annotations

from vibesys.constants import DomainName
from vibesys.orchestration.domains.base import DomainDefinition
from vibesys.orchestration.prompts import PROMPTS_DIR

DEFINITION = DomainDefinition(
    name=DomainName.LLM_SERVING,
    prompt_dir=PROMPTS_DIR / "domains" / "llm_serving",
    supports_torch_profiler=True,
)
