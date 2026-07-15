"""Shared domain metadata types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from vibe_sys.domains.environment import EnvironmentHooks


class DomainName(StrEnum):
    LLM_SERVING = "llm-serving"
    GENERIC = "generic"


class DomainRole(StrEnum):
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
    name: DomainName
    prompt_dir: Path
    environment_hooks: EnvironmentHooks
    supports_torch_profiler: bool = False
