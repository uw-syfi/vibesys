"""Multi's binding of the shared authoring kit (`vs_core.api`) to its config and identities."""

from dataclasses import dataclass
from typing import Literal

from vibesys.orchestration.multi.strategy._config import MultiConfig
from vibesys.orchestration.multi.strategy._ids import IDS, component, decision_id
from vibesys.orchestration.multi.strategy._operations import RenderRoleArtifacts
from vibesys.orchestration.multi.strategy._prompts import PromptContext
from vibesys.orchestration.multi.strategy._state import MultiStrategyState, Role, TurnRecord
from vs_core.api import (
    DecisionId,
    MeasurementPlan,
    MeasurementRecipe,
    Operation,
    OperationRequest,
    RequestTurn,
    RevisionRef,
    RunFacts,
    Scope,
    StageBudget,
    TurnShape,
    declared_operation,
)
from vs_core.api import Draft as AuthoringDraft
from vs_core.api import invocation_for as _invocation_for
from vs_core.api import request_turn as _request_turn
from vs_core.api import turn_decision_id as _turn_decision_id

type Purpose = Literal["baseline", "local-validation", "official", "profile"]


@dataclass(kw_only=True)
class Draft(AuthoringDraft[MultiStrategyState]):
    """The state and decisions one multi `decide` call is building."""

    config: MultiConfig


def operation(
    draft: Draft, identifier: DecisionId, scope: Scope, request: OperationRequest
) -> Operation:
    """A declared operation due before the run's own deadline."""
    return declared_operation(
        draft.view, identifier, scope, request, seconds=draft.config.operation_seconds
    )


def render_request(subject: str, turn: TurnRecord, context: PromptContext) -> RenderRoleArtifacts:
    """Ask the renderer for the artifacts of one role turn."""
    return RenderRoleArtifacts(subject=component(subject), ordinal=turn.serial, context=context)


def render_id(subject: str, turn: TurnRecord) -> DecisionId:
    """Decision ID of the prompt render preceding one turn."""
    return decision_id("render", subject, turn.serial)


def turn_id(subject: str, turn: TurnRecord) -> DecisionId:
    """Decision ID of one turn request."""
    return _turn_decision_id(IDS, subject, turn)


def invocation_for(role: Role, subject: str, turn: TurnRecord) -> str:
    """The logical invocation ID of a turn; a resume reuses the ID core authorized."""
    return _invocation_for(IDS, role.value, subject, turn).root


def request_turn(draft: Draft, shape: TurnShape, turn: TurnRecord, scope: Scope) -> RequestTurn:
    """Build the paid, free, correction or resume turn the record describes."""
    return _request_turn(draft.view, IDS, shape, turn, scope)


def measurement_recipe(config: MultiConfig, facts: RunFacts) -> MeasurementRecipe:
    """How this run measures a candidate: the single source of every measurement plan.

    The strategy's trusted plans and the plan an agent measures with are both built from this
    value, so an agent measurement and a trusted one share one identity.
    """
    return MeasurementRecipe(
        evaluator_digest=facts.evaluator_digest,
        workload_digest=facts.workload_digest,
        environment_digest=facts.environment_digest,
        recipe=config.recipe,
        stages=(
            StageBudget(stage_id="accuracy", execution_seconds=config.accuracy_seconds),
            StageBudget(stage_id="benchmark", execution_seconds=config.benchmark_seconds),
        ),
        queue_allowance=config.queue_allowance_seconds,
        accuracy_stage="accuracy",
    )


def measurement_plan(
    draft: Draft, candidate: RevisionRef, purpose: Purpose, stages: tuple[str, ...]
) -> MeasurementPlan:
    """An ordered plan over the named stages, each depending on the one before it."""
    return measurement_recipe(draft.config, draft.view.facts).plan(
        candidate,
        purpose=purpose,
        now_at=draft.view.run.now_at,
        stages=stages,
        submission_limit=draft.config.max_input_measurement_attempts,
    )
