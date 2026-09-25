"""Shared domain metadata types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.constants import DomainName
    from vibesys.domains.environment import EnvironmentHooks


class DomainRole(StrEnum):
    """Agent roles a domain may define prompt content for."""

    IMPLEMENTER = "implementer"
    JUDGE = "judge"
    SINGLE_AGENT = "single_agent"
    ORCHESTRATOR = "orchestrator"
    PROFILER = "profiler"


# The roles a domain can contribute to. Each maps to a ``<role>.md`` file in the
# domain prompt directory and a ``{{ domain_<role> }}`` injection point in the
# corresponding base prompt.
DOMAIN_ROLES: tuple[DomainRole, ...] = tuple(DomainRole)


@dataclass(frozen=True)
class DomainDefinition:
    """Prompt and environment metadata registered for one domain."""

    name: DomainName
    prompt_dir: Path
    environment_hooks: EnvironmentHooks
    supports_torch_profiler: bool = False
