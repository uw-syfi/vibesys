"""A scripted minimal strategy for the composition skeleton.

It plays one scenario through public core decisions only: measure the baseline,
start one attempt, request one implementer turn, measure the revision that turn
produced, settle the attempt as eligible, propose it as the winner, and stop with
the adopted result. It holds no lifecycle or accounting state of its own; each
phase advances on the strategy event that proves the previous one.
"""

from __future__ import annotations

from typing import Literal

from vs_core.api import (
    Access,
    AdoptionResult,
    ArtifactId,
    ArtifactRef,
    AttemptBudget,
    AttemptId,
    AttemptReady,
    AttemptRef,
    AttemptSettled,
    DecisionId,
    InvocationId,
    ItemId,
    Measure,
    MeasurementPlan,
    MeasurementResult,
    MeasurementStage,
    Proposal,
    ProposeWinner,
    RequestTurn,
    RetainedCandidate,
    RevisionRef,
    RoleId,
    RunResultProposal,
    RunView,
    SchemaRef,
    Scope,
    SessionId,
    SessionSpec,
    Settle,
    SettlementId,
    StartAttempt,
    Stop,
    StrategyDeclaration,
    StrategyEvent,
    StrategyId,
    StrategyState,
    TurnResult,
    TurnSpec,
    Value,
    Withdraw,
    WorkspaceMode,
    WorkspacePlan,
    WorkspaceRef,
)

ATTEMPT = AttemptRef(attempt_id=AttemptId(root="attempt-0"), generation=0)
DIGEST = "cd" * 32
DECLARATION = StrategyDeclaration(
    strategy_id=StrategyId(root="skeleton"), state_schema=SchemaRef(name="skeleton", version=1)
)

type Phase = Literal["baseline", "start", "turn", "measure", "settle", "propose", "stop", "done"]


class SkeletonState(StrategyState):
    phase: Phase = "baseline"
    candidate: RevisionRef | None = None
    settlement: SettlementId | None = None


def measurement(
    candidate: RevisionRef, purpose: Literal["baseline", "official"]
) -> MeasurementPlan:
    """An accuracy-then-benchmark plan of one exact revision."""
    return MeasurementPlan(
        purpose=purpose,
        candidate=candidate,
        evaluator_digest=DIGEST,
        workload_digest=DIGEST,
        environment_digest=DIGEST,
        stages=tuple(
            MeasurementStage(stage_id=name, execution_budget=10.0)
            for name in ("accuracy", "benchmark")
        ),
        policy="ordered",
        recipe=ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest=DIGEST),
        submitted_at=0.0,
        queue_allowance=10.0,
        deadline_at=30.0,
        accuracy_stage="accuracy",
    )


class SkeletonStrategy(Value):
    state: SkeletonState = SkeletonState(schema_version=1)
    declaration: StrategyDeclaration = DECLARATION

    def bind(self, state: SkeletonState) -> SkeletonStrategy:
        return self.model_copy(update={"state": state})

    def decide(self, view: RunView) -> Proposal[SkeletonState]:
        run = Scope(owner=view.run.run_id, generation=view.run.generation)
        attempt = Scope(owner=ATTEMPT.attempt_id, generation=0)
        state = self.state
        match state.phase:
            case "baseline":
                decision = Measure(
                    decision_id=DecisionId(root="measure-baseline"),
                    scope=run,
                    plan=measurement(view.facts.baseline, "baseline"),
                )
            case "start":
                decision = StartAttempt(
                    decision_id=DecisionId(root="start-0"),
                    scope=run,
                    attempt_id=ATTEMPT.attempt_id,
                    item_id=ItemId(root="item-0"),
                    workspace=WorkspacePlan(
                        mode=WorkspaceMode.ISOLATED_CHILD, base=view.facts.baseline
                    ),
                    budget=AttemptBudget(),
                )
            case "turn":
                decision = RequestTurn(
                    decision_id=DecisionId(root="turn-0"),
                    scope=attempt,
                    turn=TurnSpec(
                        session=SessionSpec(
                            session_id=SessionId(root="implementer"),
                            role_id=RoleId(root="implementer"),
                            policy="fresh",
                            lifetime="owner",
                            access=Access.WRITE_CANDIDATE,
                        ),
                        invocation_id=InvocationId(root="implement-0"),
                        workspace=WorkspaceRef(
                            scope=attempt,
                            revision=view.facts.baseline,
                            mode=WorkspaceMode.ISOLATED_CHILD,
                        ),
                        prompts=(),
                        output_schema=SchemaRef(name="implementation", version=1),
                        deadline_at=400.0,
                        charge_class="paid",
                    ),
                )
            case "measure":
                assert state.candidate is not None
                decision = Measure(
                    decision_id=DecisionId(root="measure-candidate"),
                    scope=attempt,
                    plan=measurement(state.candidate, "official"),
                )
            case "settle":
                decision = Withdraw(
                    decision_id=DecisionId(root="settle-0"),
                    scope=run,
                    target=ATTEMPT,
                    disposition=Settle(
                        assessments=(),
                        eligible=True,
                        retention="candidate",
                        outcome="succeeded",
                        candidate=state.candidate,
                    ),
                )
            case "propose":
                assert state.candidate is not None
                assert state.settlement is not None
                decision = ProposeWinner(
                    decision_id=DecisionId(root="winner"),
                    scope=run,
                    selection=RetainedCandidate(
                        settlement_id=state.settlement, revision=state.candidate
                    ),
                )
            case "stop":
                assert state.candidate is not None
                assert state.settlement is not None
                decision = Stop(
                    decision_id=DecisionId(root="stop"),
                    scope=run,
                    mode="drain",
                    result=RunResultProposal(
                        outcome="success",
                        reason="one attempt measured, settled and adopted",
                        selection=RetainedCandidate(
                            settlement_id=state.settlement, revision=state.candidate
                        ),
                    ),
                )
            case "done":
                return Proposal(state=state, decisions=())
        return Proposal(state=state, decisions=(decision,))

    def on_event(self, view: RunView, event: StrategyEvent) -> SkeletonState:
        del view
        return self.state.model_copy(update=self._advance(event))

    def _advance(self, event: StrategyEvent) -> dict[str, object]:
        """The state fields the event changes, if it proves the current phase done."""
        phase = _NEXT.get((type(event), self.state.phase))
        if phase is None:
            return {}
        update: dict[str, object] = {"phase": phase}
        if isinstance(event, TurnResult):
            update["candidate"] = event.observation.revision
        if isinstance(event, AttemptSettled):
            update["settlement"] = event.settlement.settlement_id
        return update


# The one event that proves each phase done: (event type, phase) -> next phase.
_NEXT: dict[tuple[type, Phase], Phase] = {
    (MeasurementResult, "baseline"): "start",
    (AttemptReady, "start"): "turn",
    (TurnResult, "turn"): "measure",
    (MeasurementResult, "measure"): "settle",
    (AttemptSettled, "settle"): "propose",
    (AdoptionResult, "propose"): "stop",
}
