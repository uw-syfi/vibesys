"""Shared domain metadata types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.constants import DomainName


class DomainRole(StrEnum):
    """Agent roles a domain may define prompt content for."""

    IMPLEMENTER = "implementer"
    JUDGE = "judge"
    SINGLE_AGENT = "single_agent"
    ORCHESTRATOR = "orchestrator"
    PROFILER = "profiler"


# The roles a domain can contribute to. Each maps to a ``<role>.md`` file in the
# domain prompt directory and the ``domain_sections[<role>]`` slot of that role's
# prompts (`core_protocol/domain_section.j2`).
DOMAIN_ROLES: tuple[DomainRole, ...] = tuple(DomainRole)


@dataclass(frozen=True)
class DomainDefinition:
    """Prompt and policy metadata registered for one domain."""

    name: DomainName
    prompt_dir: Path
    supports_torch_profiler: bool = False
    # The modalities whose prompt partials live in this pack, under
    # ``prompts/modality/<modality>/<role>.md``; a run's ``--modality`` must be one of them.
    modalities: tuple[str, ...] = ()
    # The modality a run of this domain gets when it names none.
    default_modality: str | None = None
    # Regular expressions (case-insensitive) that a task server's log tail matches when
    # its candidate caused the startup failure, such as a cache too small for the model. They
    # describe candidate failures; they do not decide whether infrastructure failures are
    # measured again. Out-of-memory is a generic fact the runtime already knows.
    candidate_startup_causes: tuple[str, ...] = ()
