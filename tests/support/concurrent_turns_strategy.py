"""A strategy that runs peer implementer turns at once in one scope and resumes every wait.

``WaitingLoopStrategy`` dispatches turns one at a time. This one dispatches a first turn
alone (it submits a measurement), then a wave of peer turns in the same attempt scope, all
in one proposal so the shell can run them together. Every suspension core accepts is
resumed by one resume turn; a turn that ends without a suspension is just a finished turn.
It records each turn's result in the order core reported them, so a test can see what the
strategy saw for a turn whose wait was refused at commit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.support.skeleton_strategy import ATTEMPT, SkeletonState, SkeletonStrategy

from vs_core.api import (
    AttemptBudget,
    DecisionId,
    InvocationId,
    Proposal,
    RequestTurn,
    ResumeAuthorized,
    Scope,
    SessionId,
    StartAttempt,
    TurnResult,
    TurnSpec,
    TurnSuspended,
)

if TYPE_CHECKING:
    from vs_core.api import RunView, StrategyEvent

FIRST = "first"
"""The invocation id of the turn that runs alone and submits the measurement."""


class ConcurrentTurnsState(SkeletonState):
    """The skeleton's state plus the turn results and suspensions seen so far."""

    results: tuple[str, ...] = ()
    """Invocation ids of the turns that finished, in the order core reported them."""
    failed: tuple[str, ...] = ()
    """Invocation ids of the finished turns whose result carries a failure."""
    refused: tuple[str, ...] = ()
    """Invocation ids of the turns whose requested wait core refused at commit."""
    suspended: tuple[str, ...] = ()
    """Invocation ids of the turns whose suspension core committed."""
    resumable: tuple[ResumeAuthorized, ...] = ()
    """Resumes core authorized, each answered by one resume turn."""


class ConcurrentTurnsStrategy(SkeletonStrategy):
    """Run ``first``, then the peers ``wave`` together, resuming every committed suspension."""

    state: ConcurrentTurnsState = ConcurrentTurnsState(schema_version=1)  # type: ignore[assignment]
    wave: tuple[str, ...] = ()

    def decide(self, view: RunView) -> Proposal[SkeletonState]:
        """Propose the start budget, the first turn, then the whole wave and the resumes."""
        state = self.state
        if state.phase == "start":
            proposal = super().decide(view)
            (start,) = proposal.decisions
            assert isinstance(start, StartAttempt)
            # One paid turn for the first and one for each peer; resumes are not paid turns.
            budget = AttemptBudget(paid_invocation_limit=1 + len(self.wave))
            return proposal.model_copy(
                update={"decisions": (start.model_copy(update={"budget": budget}),)}
            )
        if state.phase != "turn":
            return super().decide(view)
        attempt = Scope(owner=ATTEMPT.attempt_id, generation=0)
        decisions: list[RequestTurn] = []
        if FIRST not in state.results:
            decisions.append(self._turn(view, attempt, FIRST))
        else:
            decisions.extend(
                self._turn(view, attempt, name) for name in self.wave if name not in state.results
            )
        decisions.extend(
            self._resume(view, attempt, event)
            for event in state.resumable
            if event.next_invocation.invocation_id.root not in state.results
        )
        return Proposal(state=state, decisions=tuple(decisions))

    def _turn(self, view: RunView, attempt: Scope, invocation: str) -> RequestTurn:
        """A fresh turn in its own session: one session runs one turn at a time."""
        spec = self._own_session(self._spec(view, invocation), invocation)
        return RequestTurn(
            decision_id=DecisionId(root=f"turn-{invocation}"), scope=attempt, turn=spec
        )

    def _resume(self, view: RunView, attempt: Scope, event: ResumeAuthorized) -> RequestTurn:
        """The turn that continues a suspended turn once its measurements are in."""
        session = event.continuation_id.root.removesuffix("/evaluation").split("/")[0]
        spec = self._spec(view, session).model_copy(
            update={
                "invocation_id": event.next_invocation.invocation_id,
                "continuation_id": event.continuation_id,
                "charge_class": "resume",
            }
        )
        return RequestTurn(
            decision_id=DecisionId(root=f"resume-{event.continuation_id.root}"),
            scope=attempt,
            turn=self._own_session(spec, session),
        )

    @staticmethod
    def _own_session(spec: TurnSpec, session: str) -> TurnSpec:
        return spec.model_copy(
            update={
                "session": spec.session.model_copy(update={"session_id": SessionId(root=session)})
            }
        )

    def _spec(self, view: RunView, invocation: str) -> TurnSpec:
        proposal = SkeletonStrategy.decide(self, view)
        (decision,) = proposal.decisions
        assert isinstance(decision, RequestTurn)
        return decision.turn.model_copy(update={"invocation_id": InvocationId(root=invocation)})

    def on_event(self, view: RunView, event: StrategyEvent) -> SkeletonState:
        """Record results and suspensions; measure once every turn and resume has finished."""
        state = self.state
        if isinstance(event, TurnSuspended):
            name = event.continuation.invocation.invocation_id.root
            return state.model_copy(update={"suspended": (*state.suspended, name)})
        if isinstance(event, ResumeAuthorized):
            return state.model_copy(update={"resumable": (*state.resumable, event)})
        if isinstance(event, TurnResult) and state.phase == "turn":
            name = event.invocation.invocation_id.root
            update: dict[str, object] = {"results": (*state.results, name)}
            if event.failure is not None:
                update["failed"] = (*state.failed, name)
            if event.wait_refused:
                update["refused"] = (*state.refused, name)
            recorded = state.model_copy(update=update)
            expected = 1 + len(self.wave) + len(recorded.suspended)
            if len(recorded.results) < expected or len(recorded.resumable) < len(
                recorded.suspended
            ):
                return recorded
            # Every turn and every resume is in: the last result names the candidate.
            return recorded.model_copy(update=self._advance(event))
        return super().on_event(view, event)
