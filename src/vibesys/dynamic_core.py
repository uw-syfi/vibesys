"""Assemble the dynamic core policy from a run's resolved resources.

The pure policy lives in `orchestration/dynamic/core_policy`. This module does the
effects it cannot: store the evaluation recipe, read the evaluation identity,
bind the strategy's operation roles to the runtime's production owners and read
a run's committed record.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.orchestration.dynamic.core_policy.api import (
    PolicyInputs,
    build_core_policy,
    project_strategy_state,
)
from vibesys.orchestration.dynamic.strategy.api import (
    INTERPRET_KIND,
    RENDER_KIND,
    RETAIN_KIND,
    STATE_SCHEMA,
    VERIFY_KIND,
    DynamicStrategyState,
    dynamic_operation_registrations,
    dynamic_operation_registry,
)
from vibesys.plugin_registration import RuntimeRecordProjector
from vibesys.run.evaluation_backend import semantic_evaluation_identity
from vs_core.api import ArtifactId, ArtifactRef
from vs_runtime.api.core import (
    OperationCatalog,
    OperationPorts,
    OperationRole,
    RuntimeRecord,
    build_operation_catalog,
    production_owners,
)

if TYPE_CHECKING:
    from vibesys.orchestration.dynamic.core_policy.api import DynamicCorePolicy, RunBounds
    from vibesys.orchestration.dynamic.models import DynamicOptions
    from vs_runtime.api import ArtifactStore, RunFacts
    from vs_runtime.api.infrastructure import RunEnvironmentView, TrustedEvaluationPlan

# The runtime role that performs each operation kind the strategy requires.
_ROLE_OF_KIND = {
    RENDER_KIND: OperationRole.RENDER_ARTIFACTS,
    VERIFY_KIND: OperationRole.VERIFY_REVISION,
    INTERPRET_KIND: OperationRole.INTERPRET_EVIDENCE,
    RETAIN_KIND: OperationRole.RETAIN_REVISION,
}
_PLUGIN_ID = STATE_SCHEMA.name.removesuffix(".strategy-state")


@dataclass(frozen=True)
class ResolvedRun:
    """What the host resolved about a run before the core starts."""

    facts: RunFacts
    evaluation_plan: TrustedEvaluationPlan
    environment: RunEnvironmentView
    baseline_commit: str
    artifacts: ArtifactStore


def resolve_core_policy(
    options: DynamicOptions, bounds: RunBounds, run: ResolvedRun
) -> DynamicCorePolicy:
    """The core policy of one run: stores the evaluation recipe, then builds the pure policy.

    The recipe is the evaluation plan as a content-addressed artifact, and the
    three digests are the ones the evaluation backend computes for the same run.
    """
    plan = run.evaluation_plan
    content = plan.model_dump_json().encode()
    receipt = run.artifacts.write(content)
    identity = semantic_evaluation_identity(plan, run.facts, run.environment)
    return build_core_policy(
        options,
        bounds,
        PolicyInputs(
            objective=run.facts.objective,
            baseline_commit=run.baseline_commit,
            evaluator_digest=identity.evaluator.value,
            workload_digest=identity.workload.value,
            environment_digest=identity.environment.value,
            recipe=ArtifactRef(
                artifact_id=ArtifactId(root="recipe:evaluation-plan"), digest=receipt.sha256
            ),
            accuracy_configured=run.facts.accuracy_configured,
            benchmark_configured=run.facts.benchmark_configured,
            profiling=run.facts.profiler_id != "none",
            profile_measurement=run.facts.profiler_id != "none"
            and plan.profile_command is not None,
            accuracy_seconds=plan.accuracy_timeout_seconds,
            benchmark_seconds=plan.benchmark_timeout_seconds,
            profile_seconds=plan.profile_timeout_seconds,
        ),
    )


def dynamic_operation_catalog(ports: OperationPorts) -> OperationCatalog:
    """The operations the dynamic strategy requires, each owned by the production owner of its role."""
    registrations = {
        _ROLE_OF_KIND[registration.descriptor.kind]: registration
        for registration in dynamic_operation_registrations()
    }
    return build_operation_catalog(registrations, production_owners(registrations, ports))


def dynamic_projector() -> RuntimeRecordProjector[DynamicStrategyState]:
    """Read a dynamic run's strategy state from its committed runtime record."""
    return RuntimeRecordProjector(
        plugin_id=_PLUGIN_ID,
        state_type=DynamicStrategyState,
        record_type=RuntimeRecord[DynamicStrategyState],
        operations=dynamic_operation_registry(),
        project=lambda state, revision: project_strategy_state(state, experiment_revision=revision),
    )
