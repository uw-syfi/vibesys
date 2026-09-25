"""Microservices domain definition."""

from __future__ import annotations

from vibesys.constants import DomainName
from vibesys.domains.base import DomainDefinition
from vibesys.domains.environment import NoopEnvironmentHooks
from vibesys.prompts import PROMPTS_DIR  # tach-ignore(pre-existing edge)

DEFINITION = DomainDefinition(
    name=DomainName.MICROSERVICES,
    prompt_dir=PROMPTS_DIR / "domains" / "microservices",
    environment_hooks=NoopEnvironmentHooks(),
)
