"""Hypothesis lifecycle decisions for the single strategy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.agent_run.hypotheses import append_round, start_hypothesis, update_active_hypothesis

if TYPE_CHECKING:
    from vibesys.agent_run.state import AgentRunState, Hypothesis
    from vibesys.schemas import OrchestratorPlan
    from vs_loop_state.api import RoundRecord


@dataclass(frozen=True)
class PlanGuidance:
    """The plain single strategy has no component guidance."""

    def plan_prompt_context(self) -> dict[str, object]:
        """Return no optional plan variables."""
        return {}


@dataclass(frozen=True)
class HypothesisEngine:
    """Apply this strategy's hypothesis lifecycle to detached state."""

    _state: AgentRunState

    @classmethod
    def create(cls, state: AgentRunState) -> HypothesisEngine:
        """Start a lifecycle engine over recovered state."""
        return cls(state.clone())

    @property
    def state(self) -> AgentRunState:
        """Return detached authoritative state."""
        return self._state.clone()

    @property
    def guidance(self) -> PlanGuidance:
        """Return the empty plain-strategy planning guidance."""
        return PlanGuidance()

    def replace_state(self, state: AgentRunState) -> HypothesisEngine:
        """Carry this strategy over newer durable state."""
        return HypothesisEngine(state.clone())

    def start(
        self,
        plan: OrchestratorPlan,
        *,
        started_round: int,
        parent_round: int | None = None,
        parent_commit: str | None = None,
    ) -> HypothesisEngine:
        """Start the designer's new investigation."""
        return HypothesisEngine(
            start_hypothesis(
                self.state,
                plan,
                started_round=started_round,
                parent_round=parent_round,
                parent_commit=parent_commit,
            )
        )

    def complete_round(
        self, record: RoundRecord, *, next_active: Hypothesis | None
    ) -> HypothesisEngine:
        """Append one round and either retain or close the active hypothesis."""
        state = (
            update_active_hypothesis(self.state, next_active)
            if next_active is not None
            else self.state
        )
        return HypothesisEngine(append_round(state, record, keep_active=next_active is not None))
