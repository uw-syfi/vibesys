"""A tiny strategy and its world, for exercising vs-dst without a concrete product strategy.

The strategy works through its items one at a time: start an attempt (core asks the executors
for a workspace), settle it once its workspace is ready, and stop after the last item. The
world answers a workspace request with a resource named by how many workspaces it has handed
out, so the position of the world shows in the state of the run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from vs_core.api import (
    AttemptBudget,
    AttemptId,
    AttemptReady,
    AttemptRef,
    AttemptSettled,
    DecisionId,
    EnsureWorkspace,
    ItemId,
    Limits,
    OperationRegistry,
    Proposal,
    ResourceId,
    RevisionRef,
    RunEnvelope,
    RunFacts,
    RunResultProposal,
    Scope,
    Settle,
    StartAttempt,
    Stop,
    StrategyDeclaration,
    StrategyState,
    Value,
    Withdraw,
    WorkspaceMode,
    WorkspacePlan,
    initial_state,
)
from vs_core.api.testing import Harness, Succeeded
from vs_dst.api import RunSetup

if TYPE_CHECKING:
    from vs_core.api import CoreState, Decision, Request, RunView, StrategyEvent
    from vs_core.api.testing import Answer

BASELINE = RevisionRef.of_git_commit("abc123")
FACTS = RunFacts(
    objective="settle every item",
    baseline=BASELINE,
    evaluator_digest="evaluator",
    workload_digest="workload",
    environment_digest="environment",
)


class TinyState(StrategyState):
    done: int = 0
    phase: Literal["start", "settle"] = "start"


class TinyStrategy(Value):
    """Start and settle each item in turn, then stop."""

    state: TinyState
    declaration: StrategyDeclaration
    items: tuple[ItemId, ...]

    def bind(self, state: TinyState) -> TinyStrategy:
        return self.model_copy(update={"state": state})

    def decide(self, view: RunView) -> Proposal[TinyState]:
        scope = Scope(owner=view.run.run_id, generation=view.run.generation)
        index = self.state.done
        decisions: tuple[Decision, ...]
        if index == len(self.items):
            decisions = (
                Stop(
                    decision_id=DecisionId(root="finish"),
                    scope=scope,
                    mode="drain",
                    result=RunResultProposal(outcome="success", reason="every item settled"),
                ),
            )
        else:
            attempt = AttemptRef(attempt_id=AttemptId(root=f"attempt:{index}"), generation=0)
            decisions = (
                (self._start(view, scope, attempt, index),)
                if self.state.phase == "start"
                else (self._settle(scope, attempt, index),)
            )
        return Proposal(state=self.state, decisions=decisions)

    def _start(self, view: RunView, scope: Scope, attempt: AttemptRef, index: int) -> Decision:
        return StartAttempt(
            decision_id=DecisionId(root=f"start:{index}"),
            scope=scope,
            attempt_id=attempt.attempt_id,
            item_id=self.items[index],
            workspace=WorkspacePlan(mode=WorkspaceMode.EXCLUSIVE_ROOT, base=view.facts.baseline),
            budget=AttemptBudget(),
        )

    @staticmethod
    def _settle(scope: Scope, attempt: AttemptRef, index: int) -> Decision:
        return Withdraw(
            decision_id=DecisionId(root=f"settle:{index}"),
            scope=scope,
            target=attempt,
            disposition=Settle(
                assessments=(), eligible=False, retention="discard", outcome="succeeded"
            ),
        )

    def on_event(self, view: RunView, event: StrategyEvent) -> TinyState:
        del view
        if isinstance(event, AttemptReady):
            return self.state.model_copy(update={"phase": "settle"})
        if isinstance(event, AttemptSettled):
            return self.state.model_copy(update={"done": self.state.done + 1, "phase": "start"})
        return self.state


def tiny_setup(items: int) -> RunSetup:
    """The tiny strategy over ``items`` items, hosted with no operations."""
    declaration = initial_state().run.declaration
    names = tuple(ItemId(root=f"item:{n}") for n in range(items))
    return RunSetup(
        new_strategy=lambda: TinyStrategy(
            state=TinyState(schema_version=1), declaration=declaration, items=names
        ),
        harness=Harness(
            registry=OperationRegistry(()),
            facts=FACTS,
            limits=Limits(max_attempts=8, max_turns=8, max_parallel=1, max_retries=2),
            envelope_type=RunEnvelope[TinyState],
        ),
        schemas={},
    )


class TinyWorld:
    """Executors that hand out workspaces, one numbered resource after another."""

    def __init__(self, items: int = 2) -> None:
        self.setup = tiny_setup(items)
        self.workspaces_handed_out = 0
        self.seen: list[str] = []

    def __call__(self, request: Request, core: CoreState) -> Answer:
        del core
        self.seen.append(request.kind)
        if isinstance(request, EnsureWorkspace):
            number = self.workspaces_handed_out
            self.workspaces_handed_out += 1
            return Succeeded(
                resource_id=ResourceId(root=f"workspace:{number}"), revision=request.plan.base
            )
        return Succeeded()
