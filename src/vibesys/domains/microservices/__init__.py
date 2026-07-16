"""Microservices domain definition."""

from __future__ import annotations

from pathlib import Path

from vibesys.domains.base import DomainDefinition, DomainName
from vibesys.domains.environment import NoopEnvironmentHooks

DEFINITION = DomainDefinition(
    name=DomainName.MICROSERVICES,
    prompt_dir=Path(__file__).resolve().parent / "templates",
    environment_hooks=NoopEnvironmentHooks(),
)
