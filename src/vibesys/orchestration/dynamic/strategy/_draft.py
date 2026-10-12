"""Dynamic's binding of the shared authoring kit (`vs_core.api`) to its config and identities.

The kit's `Draft` and builders take every deadline and identity as an argument; this module
supplies dynamic's: the operation deadline from `DynamicConfig`, the `dyn` identity scheme,
and the prompt-render request that only dynamic's operations understand.
"""

from dataclasses import dataclass
from typing import Literal

from vibesys.orchestration.dynamic.strategy._config import DynamicConfig
from vibesys.orchestration.dynamic.strategy._ids import IDS, component, decision_id
from vibesys.orchestration.dynamic.strategy._operations import RenderRoleArtifacts
from vibesys.orchestration.dynamic.strategy._prompts import PromptContext
from vibesys.orchestration.dynamic.strategy._state import DynamicStrategyState, Role, TurnRecord
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
class Draft(AuthoringDraft[DynamicStrategyState]):
    """The state and decisions one dynamic `decide` call is building."""

    config: DynamicConfig


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


def measurement_recipe(config: DynamicConfig, facts: RunFacts) -> MeasurementRecipe:
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
            StageBudget(stage_id="profile", execution_seconds=config.profile_seconds),
        ),
        queue_allowance=config.queue_allowance_seconds,
        accuracy_stage="accuracy",
    )


def measurement_plan(
    draft: Draft,
    candidate: RevisionRef,
    purpose: Purpose,
    stages: tuple[str, ...],
    required_profile_fields: tuple[str, ...] = (),
) -> MeasurementPlan:
    """An ordered plan over the named stages, each depending on the one before it.

    ``required_profile_fields`` is what a profile's evidence must contain: a profile that asks
    for HIP API timing is not satisfied by one without it.
    """
    recipe = measurement_recipe(draft.config, draft.view.facts)
    if required_profile_fields:
        recipe = recipe.requiring("profile", required_profile_fields)
    return recipe.plan(
        candidate,
        purpose=purpose,
        now_at=draft.view.run.now_at,
        stages=stages,
        # Core retries a submission only after an infrastructure failure, and only within
        # this bound, which core's own run limit repeats (`limits_for`).
        submission_limit=draft.config.max_input_measurement_attempts,
    )
