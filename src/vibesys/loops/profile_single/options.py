"""Validated options for the profile-guided-single-agent strategy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.loops.agent import issue_board
from vibesys.loops.agent.orchestration import (
    AgentOrchestrationOptions,
    UnsupportedAgentOrchestrationError,
    options_from_descriptor,
)

if TYPE_CHECKING:
    from vs_project.api import OrchestrationDescriptor

ORCHESTRATION_ID = "profile-guided-single-agent"


class InvalidStrategyOptionsError(ValueError):
    """An option is incompatible with this strategy."""

    def __init__(self, field: str, value: object) -> None:
        """Name the incompatible strategy option and its value."""
        super().__init__(f"{ORCHESTRATION_ID}: invalid {field} option {value!r}")


def load_options(descriptor: OrchestrationDescriptor) -> AgentOrchestrationOptions:
    """Validate this strategy's identity and policy-specific settings."""
    if descriptor.id != ORCHESTRATION_ID:
        raise UnsupportedAgentOrchestrationError(descriptor.id, descriptor.config_version)
    options = options_from_descriptor(descriptor)
    if options.profile_guided is None:
        raise InvalidStrategyOptionsError("profile_guided", None)
    if options.interface not in {"inprocess", "service"}:
        raise InvalidStrategyOptionsError("interface", options.interface)
    if options.memory_layout not in issue_board.MEMORY_LAYOUTS:
        raise InvalidStrategyOptionsError("memory_layout", options.memory_layout)
    return options
