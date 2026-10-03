"""Kernel-writing domain definition."""

from __future__ import annotations

from pathlib import Path

from vibesys.constants import DomainName
from vibesys.domains.base import DomainDefinition

DEFINITION = DomainDefinition(
    name=DomainName.KERNEL_WRITING,
    prompt_dir=Path(__file__).resolve().parent / "prompts",
)
