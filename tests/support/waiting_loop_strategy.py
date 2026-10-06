"""A strategy that dispatches generated fresh agent turns and resumes every suspension.

Shared by the composition smoke and the cheap property of D396: both need turns that may
yield to an evaluation wait and a strategy that resumes each yield exactly once.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from tests.support.skeleton_strategy import ATTEMPT, SkeletonState, SkeletonStrategy

from vs_core.api import (
    AttemptBudget,
    DecisionId,
    InvocationId,
    MeasurementResult,
    Proposal,
    RequestTurn,
    ResumeAuthorized,
    Scope,
    StartAttempt,
    TurnResult,
    TurnSpec,
    TurnSuspended,
)

if TYPE_CHECKING:
    from vs_core.api import RunView, StrategyEvent


class LoopState(SkeletonState):
    """The skeleton's state plus how far the generated turn schedule has run."""

    fresh: int = 0
    yielded: bool = False
    suspensions: int = 0
    resumes: int = 0
    resume: ResumeAuthorized | None = None


class LoopStrategy(SkeletonStrategy):
    """Dispatch ``total`` fresh implementer turns, resuming every suspension, then measure."""

    state: LoopState = LoopState(schema_version=1)  # type: ignore[assignment]
    total: int = 1

    def decide(self, view: RunView) -> Proposal[SkeletonState]:
        """Resume the authorized turn, hold while suspended, else dispatch the next turn."""
        state = self.state
        if state.phase == "start":
            proposal = super().decide(view)
            (start,) = proposal.decisions
            assert isinstance(start, StartAttempt)
            budget = AttemptBudget(paid_invocation_limit=self.total)
            return proposal.model_copy(
                update={"decisions": (start.model_copy(update={"budget": budget}),)}
            )
        if state.phase != "turn":
            return super().decide(view)
        attempt = Scope(owner=ATTEMPT.attempt_id, generation=0)
        if state.resume is not None:
            event = state.resume
            spec = self._spec(view, "implement-0").model_copy(
                update={
                    "invocation_id": event.next_invocation.invocation_id,
                    "continuation_id": event.continuation_id,
                    "charge_class": "resume",
                }
            )
            decision = RequestTurn(
                decision_id=DecisionId(root=f"turn-resume-{state.resumes}"),
                scope=attempt,
                turn=spec,
            )
            return Proposal(state=state, decisions=(decision,))
        if state.yielded:
            return Proposal(state=state, decisions=())
        decision = RequestTurn(
            decision_id=DecisionId(root=f"turn-{state.fresh}"),
            scope=attempt,
            turn=self._spec(view, f"implement-{state.fresh}"),
        )
        return Proposal(state=state, decisions=(decision,))

    def _spec(self, view: RunView, invocation: str) -> TurnSpec:
        proposal = SkeletonStrategy.decide(self, view)
        (decision,) = proposal.decisions
        assert isinstance(decision, RequestTurn)
        return decision.turn.model_copy(update={"invocation_id": InvocationId(root=invocation)})

    def on_event(self, view: RunView, event: StrategyEvent) -> SkeletonState:
        """Count suspensions and resumes; each finished turn schedules the next or measures."""
        state = self.state
        if (folded := self._fold(event)) is not None:
            return folded
        if isinstance(event, TurnResult) and state.phase == "turn":
            reply = json.loads(event.output_json or "{}")
            fresh = state.fresh + (state.resume is None)
            if reply.get("waiting"):
                return state.model_copy(update={"yielded": True, "resume": None, "fresh": fresh})
            if fresh < self.total:
                return state.model_copy(update={"fresh": fresh, "resume": None})
            return super().on_event(view, event).model_copy(update={"resume": None, "fresh": fresh})
        return super().on_event(view, event)

    def _fold(self, event: StrategyEvent) -> LoopState | None:
        """The state after an event about suspensions or an agent's own measurement, or None."""
        state = self.state
        if isinstance(event, MeasurementResult) and any(
            item.purpose == "local-validation" for item in event.evidence
        ):
            # An agent's own measurement reports here too; it is not the run's candidate.
            return state
        if isinstance(event, TurnSuspended):
            return state.model_copy(update={"suspensions": state.suspensions + 1})
        if isinstance(event, ResumeAuthorized):
            return state.model_copy(
                update={"resumes": state.resumes + 1, "resume": event, "yielded": False}
            )
        return None
