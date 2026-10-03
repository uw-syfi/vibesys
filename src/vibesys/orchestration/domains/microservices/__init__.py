"""Microservices domain definition."""

from __future__ import annotations

from vibesys.constants import DomainName
from vibesys.orchestration.domains.base import DomainDefinition
from vibesys.prompts import PROMPTS_DIR

DEFINITION = DomainDefinition(
    name=DomainName.MICROSERVICES,
    prompt_dir=PROMPTS_DIR / "domains" / "microservices",
)
