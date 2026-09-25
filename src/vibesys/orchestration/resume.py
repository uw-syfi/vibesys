"""Neutral contracts for versioned orchestration persistence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vs_project.api import OrchestrationDescriptor


@dataclass(frozen=True, slots=True)
class OrchestrationResumeDecision:
    """An owner-approved descriptor update and its workspace precondition."""

    descriptor: OrchestrationDescriptor | None
    requires_clean_workspace: bool = False
