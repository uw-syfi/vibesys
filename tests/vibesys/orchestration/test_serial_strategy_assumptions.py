"""Assumption A1 of the strategy port design, on the real core.

A serial strategy that audits only some rounds (`official_eval_every > 1`) settles a
skipped round as a buildable parent that can never be adopted, and starts the next round on
it. That needs core to (1) accept a settlement with `retention="candidate"`, `eligible=False`
and no assessments, and (2) accept a later `StartAttempt` whose base is that revision.
The run is driven by the real `step` and `project` with scripted executors, as the dynamic
strategy's tests do, so a refusal by core shows up as a `Rejected` event.
"""

from __future__ import annotations

import hashlib
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict

from vs_core.api import (
    Access,
    AttemptBudget,
    AttemptId,
    AttemptReady,
    AttemptRef,
    AttemptSettled,
    AttemptView,
    CloseSession,
    CoreState,
    DecisionId,
    DispatchTurn,
    EnsureSession,
    EnsureWorkspace,
    InvocationId,
    ItemId,
    Limits,
    OperationRegistry,
    Proposal,
    Rejected,
    Request,
    RequestTurn,
    ResourceId,
    RetainRevision,
    RevisionRef,
    RoleId,
    RunFacts,
    RunResultProposal,
    RunStatus,
    RunView,
    SchemaRef,
    Scope,
    SessionId,
    SessionSpec,
    Settle,
    SnapshotAndRetain,
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
from vs_core.testing.drive import Answer, Harness, Succeeded
from vs_runtime.api.testing import Run, drive_shell

type Retention = Literal["wip", "candidate"]

ATTEMPTS = 2
LIMITS = Limits(max_attempts=ATTEMPTS, max_turns=4, max_parallel=1, max_retries=1, max_refunds=1)
BASE = RevisionRef.of_git_commit("base")


SCHEMA = SchemaRef(name="opaque-result", version=1)


class Opaque(BaseModel):
    """A reply the strategy does not read."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SerialState(StrategyState):
    phase: Literal["start", "turn", "settle", "finish", "done"] = "start"
    index: int = 0
    retained: tuple[RevisionRef, ...] = ()
    """The candidate each settled attempt kept, in order."""


class Serial(Value):
    """Runs `ATTEMPTS` attempts one after another; each starts on the previous one's candidate."""

    state: SerialState
    declaration: StrategyDeclaration
    mode: WorkspaceMode
    retention: Retention

    def bind(self, state: SerialState) -> Serial:
        return self.model_copy(update={"state": state})

    def _attempt(self) -> AttemptId:
        return AttemptId(root=f"attempt:{self.state.index}")

    def decide(self, view: RunView) -> Proposal[SerialState]:
        state, run = self.state, Scope(owner=view.run.run_id, generation=view.run.generation)
        attempt = self._attempt()
        scope = Scope(owner=attempt, generation=0)
        base = state.retained[-1] if state.retained else view.facts.baseline
        if state.phase == "start":
            decision = StartAttempt(
                decision_id=DecisionId(root=f"start:{state.index}"),
                scope=run,
                attempt_id=attempt,
                item_id=ItemId(root=f"item:{state.index}"),
                workspace=WorkspacePlan(mode=self.mode, base=base),
                budget=AttemptBudget(),
            )
            return Proposal(state=state, decisions=(decision,))
        if state.phase == "turn":
            decision = RequestTurn(
                decision_id=DecisionId(root=f"turn:{state.index}"),
                scope=scope,
                turn=TurnSpec(
                    session=SessionSpec(
                        session_id=SessionId(root=f"session:{state.index}"),
                        role_id=RoleId(root="worker"),
                        policy="reuse",
                        lifetime="owner",
                        access=Access.WRITE_CANDIDATE,
                    ),
                    invocation_id=InvocationId(root=f"invocation:{state.index}"),
                    workspace=WorkspaceRef(scope=scope, revision=base, mode=self.mode),
                    prompts=(),
                    output_schema=SCHEMA,
                    deadline_at=1000.0,
                    charge_class="paid",
                ),
            )
        elif state.phase == "settle":
            live = next(item for item in view.attempts if item.attempt_id == attempt)
            if not live.checkpoints:
                # The write turn's snapshot is still in flight: settle once it is a checkpoint.
                return Proposal(state=state, decisions=())
            candidate = live.checkpoints[-1].revision
            decision = Withdraw(
                decision_id=DecisionId(root=f"settle:{state.index}"),
                scope=run,
                target=_ref(live),
                disposition=Settle(
                    assessments=(),
                    eligible=False,
                    retention=self.retention,
                    outcome="succeeded",
                    candidate=candidate,
                ),
            )
        elif state.phase == "finish":
            decision = Stop(
                decision_id=DecisionId(root="finish"),
                scope=run,
                mode="drain",
                result=RunResultProposal(outcome="success", reason="serial attempts done"),
            )
        else:
            return Proposal(state=state, decisions=())
        return Proposal(state=state, decisions=(decision,))

    def on_event(self, view: RunView, event: StrategyEvent) -> SerialState:
        del view
        state = self.state
        if isinstance(event, AttemptReady):
            return state.model_copy(update={"phase": "turn"})
        if isinstance(event, TurnResult):
            return state.model_copy(update={"phase": "settle"})
        if isinstance(event, AttemptSettled) and event.settlement.candidate is not None:
            index = state.index + 1
            return state.model_copy(
                update={
                    "index": index,
                    "phase": "finish" if index == ATTEMPTS else "start",
                    "retained": (*state.retained, event.settlement.candidate),
                }
            )
        return state


def _ref(attempt: AttemptView) -> AttemptRef:
    return AttemptRef(attempt_id=attempt.attempt_id, generation=attempt.generation)


def _commit(name: str) -> RevisionRef:
    return RevisionRef.of_git_commit(hashlib.sha256(name.encode()).hexdigest()[:40])


def executors(request: Request, _core: CoreState) -> Answer:
    """What a workspace and an agent executor report, with a fresh commit per snapshot."""
    answer: Answer = Succeeded()
    match request:
        case EnsureWorkspace():
            resource = ResourceId(root=f"workspace:{request.attempt.attempt_id.root}")
            answer = Succeeded(resource_id=resource, revision=request.plan.base)
        case EnsureSession():
            answer = Succeeded(resource_id=_lease(request.spec.session_id))
        case CloseSession():
            answer = Succeeded(resource_id=_lease(request.session_id))
        case DispatchTurn():
            answer = Succeeded(
                output_json="{}", resource_id=_lease(request.turn.session.session_id)
            )
        case SnapshotAndRetain():
            answer = Succeeded(revision=_commit(request.attempt.attempt_id.root))
        case RetainRevision():
            answer = Succeeded(revision=request.revision)
    return answer


def _lease(session: SessionId) -> ResourceId:
    return ResourceId(root=f"lease:{session.root}")


def run(mode: WorkspaceMode, retention: Retention) -> Run:
    state = SerialState(schema_version=1)
    declaration = StrategyDeclaration(
        strategy_id=StrategyId(root="serial"), state_schema=SchemaRef(name="serial", version=1)
    )
    strategy = Serial(state=state, declaration=declaration, mode=mode, retention=retention)
    facts = RunFacts(
        objective="serial",
        baseline=BASE,
        evaluator_digest="evaluator",
        workload_digest="workload",
        environment_digest="environment",
    )
    harness = Harness(registry=OperationRegistry(), facts=facts, limits=LIMITS)
    return drive_shell(strategy, executors, harness, {SCHEMA: Opaque})


@pytest.mark.parametrize("mode", [WorkspaceMode.ISOLATED_CHILD, WorkspaceMode.EXCLUSIVE_ROOT])
@pytest.mark.parametrize("retention", ["candidate", "wip"])
def test_a_skipped_round_is_kept_as_a_parent_and_the_next_round_builds_on_it(
    mode: WorkspaceMode, retention: Retention
) -> None:
    """`candidate` is the design; `wip` is its documented fallback, and works too."""
    finished = run(mode, retention)
    assert finished.halted is None
    assert finished.core.run.status is RunStatus.TERMINAL
    refused = [
        item.feedback for item in finished.core.run.receipts if isinstance(item.feedback, Rejected)
    ]
    assert refused == []
    settled = finished.core.settlement.settlements
    assert len(settled) == ATTEMPTS
    assert all(
        not item.eligible and item.retention == retention and item.assessments == ()
        for item in settled
    )
    bases = [item.workspace.base for item in finished.core.attempts.attempts]
    assert bases == [BASE, settled[0].candidate]
