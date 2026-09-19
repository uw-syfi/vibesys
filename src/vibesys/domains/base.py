"""Shared domain metadata types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path  # noqa: TC003  # tracked: #288

from vibesys.constants import DomainName  # noqa: TC001  # tracked: #288
from vibesys.domains.environment import EnvironmentHooks  # noqa: TC001  # tracked: #288


class DomainRole(StrEnum):  # noqa: D101  # tracked: #288
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
class DomainDefinition:  # noqa: D101  # tracked: #288
    name: DomainName
    prompt_dir: Path
    environment_hooks: EnvironmentHooks
    supports_torch_profiler: bool = False
