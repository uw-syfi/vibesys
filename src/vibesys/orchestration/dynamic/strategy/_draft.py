"""Scratch space of one `decide` call and the core value builders shared by subjects.

`Draft` accumulates ordered decisions and the evolving state inside a single pure
call; nothing escapes it except the final `Proposal`. The builders turn strategy
intent into core `Scope`, `TurnSpec`, `Operation` and `MeasurementPlan` values
using only the `RunView` and the static `DynamicConfig`, so identical inputs
always propose byte-identical decisions.
"""

from dataclasses import dataclass, field
from typing import Literal

from vibesys.orchestration.dynamic.strategy._config import DynamicConfig
from vibesys.orchestration.dynamic.strategy._ids import (
    decision_id,
    invocation_id,
    role_id,
    session_id,
)
from vibesys.orchestration.dynamic.strategy._operations import RenderRoleArtifacts
from vibesys.orchestration.dynamic.strategy._prompts import PromptContext
from vibesys.orchestration.dynamic.strategy._state import DynamicStrategyState, Role, TurnRecord
from vs_core.api import (
    Access,
    AttemptId,
    Decision,
    DecisionId,
    InvocationId,
    MeasurementPlan,
    MeasurementStage,
    Operation,
    OperationRequest,
    RequestTurn,
    RevisionRef,
    RunView,
    SchemaRef,
    Scope,
    SessionSpec,
    TurnSpec,
    WorkspaceMode,
    WorkspaceRef,
)

type Purpose = Literal["baseline", "local-validation", "official", "profile"]


@dataclass
class Draft:
    """The state and decisions one `decide` call is building."""

    view: RunView
    config: DynamicConfig
    state: DynamicStrategyState
    decisions: list[Decision] = field(default_factory=list)

    def emit(self, decision: Decision) -> None:
        """Append one decision in proposal order."""
        self.decisions.append(decision)

    def update(self, **fields: object) -> None:
        """Replace state fields without mutating the previous state."""
        self.state = self.state.model_copy(update=fields)


def run_scope(view: RunView) -> Scope:
    """The run's own scope at its current generation."""
    return Scope(owner=view.run.run_id, generation=view.run.generation)


def attempt_scope(view: RunView, attempt: AttemptId, generation: int) -> Scope:
    """An attempt's scope; core reports the live generation, a new attempt starts at 0."""
    live = next((item for item in view.attempts if item.attempt_id == attempt), None)
    return Scope(owner=attempt, generation=generation if live is None else live.generation)


def operation(
    draft: Draft, identifier: DecisionId, scope: Scope, request: OperationRequest
) -> Operation:
    """A declared operation due before the run's own deadline."""
    return Operation(
        decision_id=identifier,
        scope=scope,
        request=request,
        deadline_at=draft.view.run.now_at + draft.config.operation_seconds,
    )


def render_request(subject: str, turn: TurnRecord, context: PromptContext) -> RenderRoleArtifacts:
    """Ask the renderer for the artifacts of one role turn."""
    return RenderRoleArtifacts(subject=subject, ordinal=turn.serial, context=context)


def render_id(subject: str, turn: TurnRecord) -> DecisionId:
    """Decision ID of the prompt render preceding one turn."""
    return decision_id("render", subject, turn.serial)


def turn_id(subject: str, turn: TurnRecord) -> DecisionId:
    """Decision ID of one turn request."""
    return decision_id("turn", subject, turn.serial)


def invocation_for(role: Role, subject: str, turn: TurnRecord) -> str:
    """The logical invocation ID of a turn; a resume reuses the ID core authorized."""
    if turn.resume_as is not None:
        return turn.resume_as.root
    return invocation_id(role.value, subject, turn.serial).root


def session_for(role: Role, subject: str, access: Access, *, reuse: bool) -> SessionSpec:
    """The conversation a role speaks in; continuations reuse their subject's session."""
    return SessionSpec(
        session_id=session_id(role.value, subject),
        role_id=role_id(role.value),
        policy="reuse" if reuse else "fresh",
        lifetime="owner",
        access=access,
    )


def workspace_for(scope: Scope, revision: RevisionRef, mode: WorkspaceMode) -> WorkspaceRef:
    """The exact revision a turn works on and how it may touch it."""
    return WorkspaceRef(scope=scope, revision=revision, mode=mode)


@dataclass(frozen=True)
class TurnShape:
    """Who runs a turn, on what, with which access, and what it must answer with."""

    role: Role
    subject: str
    workspace: WorkspaceRef | Scope
    access: Access
    reuse: bool
    output_schema: SchemaRef
    seconds: float


def request_turn(draft: Draft, shape: TurnShape, turn: TurnRecord, scope: Scope) -> RequestTurn:
    """Build the paid, free, correction or resume turn the record describes."""
    role, subject = shape.role, shape.subject
    return RequestTurn(
        decision_id=turn_id(subject, turn),
        scope=scope,
        turn=TurnSpec(
            session=session_for(role, subject, shape.access, reuse=shape.reuse),
            invocation_id=InvocationId(root=invocation_for(role, subject, turn)),
            continuation_id=turn.continuation,
            workspace=shape.workspace,
            prompts=turn.prompts,
            output_schema=shape.output_schema,
            tool_policy=turn.tool_policy,
            deadline_at=draft.view.run.now_at + shape.seconds,
            charge_class=turn.charge,
            predecessor=turn.invocation,
        ),
    )


def measurement_plan(
    draft: Draft, candidate: RevisionRef, purpose: Purpose, stages: tuple[str, ...]
) -> MeasurementPlan:
    """An ordered plan over the named stages, each depending on the one before it."""
    config = draft.config
    seconds = {
        "accuracy": config.accuracy_seconds,
        "benchmark": config.benchmark_seconds,
        "profile": config.profile_seconds,
    }
    ordered = tuple(
        MeasurementStage(
            stage_id=name,
            depends_on=stages[index - 1 : index] if index else (),
            execution_budget=seconds[name],
        )
        for index, name in enumerate(stages)
    )
    now = draft.view.run.now_at
    facts = draft.view.facts
    return MeasurementPlan(
        purpose=purpose,
        candidate=candidate,
        evaluator_digest=facts.evaluator_digest,
        workload_digest=facts.workload_digest,
        environment_digest=facts.environment_digest,
        stages=ordered,
        policy="ordered",
        recipe=config.recipe,
        submitted_at=now,
        queue_allowance=config.queue_allowance_seconds,
        deadline_at=now
        + sum(item.execution_budget for item in ordered)
        + config.queue_allowance_seconds,
    )
