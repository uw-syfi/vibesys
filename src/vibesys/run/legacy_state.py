"""Compatibility state handle for the built-in agent completed-round ledger."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.run.agent_round_compat import LegacyAgentRoundStore
from vibesys.run.state import RunState

if TYPE_CHECKING:
    from vs_loop_state.api import RoundRecord


@dataclass(frozen=True)
class LegacyRunState(RunState):
    """Add the retired agent ledger read expected by existing loop callers."""

    def completed_rounds(self) -> list[RoundRecord]:
        """Load the former agent completed-round history."""
        return LegacyAgentRoundStore(self.project, self.run_id).load()
