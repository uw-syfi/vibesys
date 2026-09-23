"""Built-in agent round preparation policies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vibesys.loops.agent.policy_support import (
    _INNER_LOOPS,
    _CarryOver,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.domains.base import DomainDefinition
    from vibesys.loops.agent.roles import BuiltInAgentRoles
    from vibesys.run import LoopContext
    from vibesys.schemas import ProfilerSummary, SingleAgentRoundResponse
    from vs_loop_state.api import RoundRecord


@dataclass(frozen=True)
class RoundPreparationServices:
    """Resources shared by the built-in round preparation policies."""

    ctx: LoopContext
    agents: BuiltInAgentRoles
    objective: str
    modality: str | None
    interface: str
    domain_definition: DomainDefinition
    progress_path: Path
    progress_location: str


@dataclass(frozen=True)
class RoundPreparationRequest:
    """Round evidence available before the designer chooses a new hypothesis."""

    round_number: int
    records: list[RoundRecord]
    carry: _CarryOver
    previous_single_response: SingleAgentRoundResponse | None


class RoundPreparation(Protocol):
    """Select the evidence given to the designer before a new hypothesis."""

    def profiler_summary(self, request: RoundPreparationRequest) -> ProfilerSummary | None:
        """Return fresh or carried profiler evidence for the round."""
        ...


def validate_inner_policy(inner_loop: str) -> None:
    """Reject unknown built-in agent strategies before run resources are opened."""
    if inner_loop not in _INNER_LOOPS:
        message = f"Unknown inner_loop {inner_loop!r}; choose from {', '.join(_INNER_LOOPS)}"
        raise ValueError(message)
