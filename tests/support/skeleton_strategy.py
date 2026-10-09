"""A scripted minimal strategy for the composition skeleton.

It plays one scenario through public core decisions only: measure the baseline,
start one attempt, request one implementer turn, measure the revision that turn
produced, settle the attempt as eligible, propose it as the winner, and stop with
the adopted result. It holds no lifecycle or accounting state of its own; each
phase advances on the strategy event that proves the previous one.
"""

from __future__ import annotations

import json
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
    Decision,
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
    TrustedBaseline,
    TurnResult,
    TurnSpec,
    Value,
    Withdraw,
    WorkspaceMode,
    WorkspacePlan,
    WorkspaceRef,
)

ATTEMPT = AttemptRef(attempt_id=AttemptId(root="attempt-0"), generation=0)
DIGEST = "ab" * 32
DECLARATION = StrategyDeclaration(
    strategy_id=StrategyId(root="skeleton"), state_schema=SchemaRef(name="skeleton", version=1)
)

type Phase = Literal[
    "baseline", "start", "turn", "measure", "settle", "propose", "stop", "cancel", "failed", "done"
]


class SkeletonState(StrategyState):
    phase: Phase = "baseline"
    candidate: RevisionRef | None = None
    settlement: SettlementId | None = None
    failure: str | None = None


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
        queue_allowance=880.0,
        # Absolute run-clock time, past any restart the scenarios make (the run ends at 1000 s).
        deadline_at=900.0,
        accuracy_stage="accuracy",
    )


class SkeletonStrategy(Value):
    """The scripted scenario; ``measured=False`` omits both measurements.

    The unmeasured variant starts at the attempt and settles right after the turn, so
    the workspace, session, retirement and adoption interfaces can be driven while the
    measurement interfaces are still missing pieces. Its settlement keeps the candidate
    but is ineligible (no evidence), so the run adopts the trusted baseline.
    """

    state: SkeletonState = SkeletonState(schema_version=1)
    declaration: StrategyDeclaration = DECLARATION
    measured: bool = True
    keeps_candidate: bool = True
    cancels_after_start: bool = False

    @classmethod
    def unmeasured(cls, *, keeps_candidate: bool = True) -> SkeletonStrategy:
        """The scenario without the baseline and candidate measurements.

        With ``keeps_candidate=False`` the attempt is discarded and the trusted
        baseline is adopted, which needs no retained revision.
        """
        return cls(
            state=SkeletonState(schema_version=1, phase="start"),
            measured=False,
            keeps_candidate=keeps_candidate,
        )

    @classmethod
    def cancelled(cls) -> SkeletonStrategy:
        """Start one attempt and, once it is ready, stop the run with ``cancel``.

        No session or measurement is involved: the attempt's workspace is acquired and
        then closed and discarded by core, and the run ends with no adopted revision.
        """
        return cls(
            state=SkeletonState(schema_version=1, phase="start"),
            measured=False,
            cancels_after_start=True,
        )

    def _selection(self, view: RunView) -> RetainedCandidate | TrustedBaseline:
        state = self.state
        settlement = next(
            (row for row in view.settlements if row.settlement_id == state.settlement), None
        )
        # A retained candidate wins only on an eligible settlement, and core makes one eligible
        # only with accepted evidence for that candidate. Without a measurement there is none,
        # so the trusted baseline is the only revision this strategy may adopt.
        if self.keeps_candidate and settlement is not None and settlement.eligible:
            assert state.candidate is not None
            return RetainedCandidate(
                settlement_id=settlement.settlement_id, revision=state.candidate
            )
        return TrustedBaseline(revision=view.facts.baseline)

    def bind(self, state: SkeletonState) -> SkeletonStrategy:
        return self.model_copy(update={"state": state})

    def decide(self, view: RunView) -> Proposal[SkeletonState]:
        run = Scope(owner=view.run.run_id, generation=view.run.generation)
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
            case "propose":
                decision = ProposeWinner(
                    decision_id=DecisionId(root="winner"),
                    scope=run,
                    selection=self._selection(view),
                )
            case "stop":
                decision = Stop(
                    decision_id=DecisionId(root="stop"),
                    scope=run,
                    mode="drain",
                    result=RunResultProposal(
                        outcome="success",
                        reason="one attempt measured, settled and adopted",
                        selection=self._selection(view),
                    ),
                )
            case "cancel":
                decision = Stop(
                    decision_id=DecisionId(root="stop-cancel"),
                    scope=run,
                    mode="cancel",
                    result=RunResultProposal(outcome="cancelled", reason="attempt cancelled"),
                )
            case "failed":
                decision = Stop(
                    decision_id=DecisionId(root="stop-failed"),
                    scope=run,
                    mode="cancel",
                    result=RunResultProposal(outcome="failure", reason=state.failure or "failed"),
                )
            case "done":
                return Proposal(state=state, decisions=())
            case _:
                decision = self._attempt_decision(view)
        return Proposal(state=state, decisions=(decision,))

    def _attempt_decision(self, view: RunView) -> Decision:
        """The decision of a phase that acts on the attempt (turn, measure, settle)."""
        run = Scope(owner=view.run.run_id, generation=view.run.generation)
        attempt = Scope(owner=ATTEMPT.attempt_id, generation=0)
        state = self.state
        match state.phase:
            case "turn":
                return RequestTurn(
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
                return Measure(
                    decision_id=DecisionId(root="measure-candidate"),
                    scope=attempt,
                    plan=measurement(state.candidate, "official"),
                )
            case "settle":
                return Withdraw(
                    decision_id=DecisionId(root="settle-0"),
                    scope=run,
                    target=ATTEMPT,
                    disposition=Settle(
                        assessments=(),
                        eligible=self.keeps_candidate,
                        retention="candidate" if self.keeps_candidate else "discard",
                        outcome="succeeded" if self.keeps_candidate else "failed",
                        candidate=state.candidate if self.keeps_candidate else None,
                    ),
                )
            case _:
                raise AssertionError(state.phase)

    def on_event(self, view: RunView, event: StrategyEvent) -> SkeletonState:
        del view
        return self.state.model_copy(update=self._advance(event))

    def _advance(self, event: StrategyEvent) -> dict[str, object]:
        """The state fields the event changes, if it proves the current phase done."""
        if isinstance(event, MeasurementResult) and event.failure is not None:
            return {"phase": "failed", "failure": f"measurement {event.failure.value}"}
        phase = _NEXT.get((type(event), self.state.phase))
        if phase == "turn" and self.cancels_after_start:
            phase = "cancel"
        if phase == "measure" and not self.measured:
            phase = "settle"
        if phase is None:
            return {}
        update: dict[str, object] = {"phase": phase}
        if isinstance(event, TurnResult):
            update["candidate"] = _committed(event)
            if update["candidate"] is None:
                # A turn that never ran (or committed nothing) has no revision to measure.
                update.update(phase="failed", failure="the implementer turn produced no commit")
        if isinstance(event, AttemptSettled):
            update["settlement"] = event.settlement.settlement_id
        return update


def _committed(event: TurnResult) -> RevisionRef | None:
    """The commit the implementer reports in its structured reply, if the turn produced one."""
    if event.output_json is None:
        return None
    commit = json.loads(event.output_json).get("commit")
    return RevisionRef.of_git_commit(commit) if commit else None


# The one event that proves each phase done: (event type, phase) -> next phase.
_NEXT: dict[tuple[type, Phase], Phase] = {
    (MeasurementResult, "baseline"): "start",
    (AttemptReady, "start"): "turn",
    (TurnResult, "turn"): "measure",
    (MeasurementResult, "measure"): "settle",
    (AttemptSettled, "settle"): "propose",
    (AdoptionResult, "propose"): "stop",
}
