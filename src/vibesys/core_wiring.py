"""Host wiring every strategy on the core lifecycle shares.

A strategy's pure policy needs values only the host can produce: the evaluation recipe stored
as a content-addressed artifact, the run's evaluation identity as the backend computes it, the
stage budgets and gates the plan declares, and the production owner of each operation role.
This module resolves them once, so a strategy's own wiring shrinks to the calls that are its
own (its options, its roles, its plan).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.run.evaluation_backend import semantic_evaluation_identity
from vs_core.api import ArtifactId, ArtifactRef
from vs_runtime.api.core import (
    AgentEvaluationPolicy,
    OperationCatalog,
    build_operation_catalog,
    operation_roles,
    production_owners,
)

if TYPE_CHECKING:
    from vs_core.api import MeasurementRecipe, StrategyOperations
    from vs_runtime.api import ArtifactStore, RunFacts
    from vs_runtime.api.core import OperationPorts
    from vs_runtime.api.infrastructure import RunEnvironmentView, TrustedEvaluationPlan

__all__ = [
    "EvaluationInputs",
    "ParallelCandidateFacts",
    "ResolvedRun",
    "agent_evaluation_policy",
    "evaluation_inputs",
    "operation_catalog",
    "require_candidate_sandboxes",
]


class ParallelCandidateFacts(Protocol):
    """What the sandbox check reads of a run environment."""

    @property
    def env_kind(self) -> str:
        """The coarse environment label, for diagnostics."""
        ...

    @property
    def parallel_candidate_obstacle(self) -> str | None:
        """Why the environment cannot open isolated candidate sandboxes, or None."""
        ...


@dataclass(frozen=True)
class ResolvedRun:
    """What the host resolved about a run before the core starts."""

    facts: RunFacts
    evaluation_plan: TrustedEvaluationPlan
    environment: RunEnvironmentView
    baseline_commit: str
    artifacts: ArtifactStore


@dataclass(frozen=True)
class EvaluationInputs:
    """How a run is evaluated, as plain values a pure policy can read.

    The digests are the evaluator, workload and environment identities as the evaluation
    backend computes them for the same run. ``recipe`` names the stored evaluation plan every
    measurement plan cites. A stage budget of None means the plan declares no timeout for it.
    """

    evaluator_digest: str
    workload_digest: str
    environment_digest: str
    recipe: ArtifactRef
    accuracy_configured: bool
    benchmark_configured: bool
    profiling: bool
    profile_measurement: bool
    accuracy_seconds: float | None
    benchmark_seconds: float | None
    profile_seconds: float | None


def evaluation_inputs(run: ResolvedRun) -> EvaluationInputs:
    """Store the evaluation recipe and read the run's evaluation identity and budgets."""
    plan = run.evaluation_plan
    receipt = run.artifacts.write(plan.model_dump_json().encode())
    identity = semantic_evaluation_identity(plan, run.facts, run.environment)
    profiling = run.facts.profiler_id != "none"
    return EvaluationInputs(
        evaluator_digest=identity.evaluator.value,
        workload_digest=identity.workload.value,
        environment_digest=identity.environment.value,
        recipe=ArtifactRef(
            artifact_id=ArtifactId(root="recipe:evaluation-plan"), digest=receipt.sha256
        ),
        accuracy_configured=run.facts.accuracy_configured,
        benchmark_configured=run.facts.benchmark_configured,
        profiling=profiling,
        profile_measurement=profiling and plan.profile_command is not None,
        accuracy_seconds=plan.accuracy_timeout_seconds,
        benchmark_seconds=plan.benchmark_timeout_seconds,
        profile_seconds=plan.profile_timeout_seconds,
    )


def agent_evaluation_policy(
    recipe: MeasurementRecipe, *, accuracy: bool, benchmark: bool
) -> AgentEvaluationPolicy:
    """The plan an agent measures with: the run's configured gates, once each.

    It is built from the same recipe as the strategy's own trusted measurement plans, so an
    agent measurement and a trusted one share one identity.
    """
    configured = tuple(
        name for name, enabled in (("accuracy", accuracy), ("benchmark", benchmark)) if enabled
    )
    return AgentEvaluationPolicy.from_recipe(recipe.only(configured))


def operation_catalog(operations: StrategyOperations, ports: OperationPorts) -> OperationCatalog:
    """The operations a strategy requires, each owned by the production owner of its role."""
    registrations = operation_roles(operations)
    return build_operation_catalog(registrations, production_owners(registrations, ports))


def require_candidate_sandboxes(environment: ParallelCandidateFacts, *, loop: str) -> None:
    """Refuse a run environment that cannot open isolated candidate sandboxes.

    A strategy whose workstreams each run in their own candidate sandbox would otherwise fail
    its input measurement and pay for work that cannot start.
    """
    obstacle = environment.parallel_candidate_obstacle
    if obstacle is not None:
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code=f"{loop}_run_environment_unsupported",
                stage="run_environment_validation",
                message=(
                    f"the {loop} loop cannot run on the {environment.env_kind!r} run "
                    "environment: it cannot open isolated candidate sandboxes (parallel "
                    f"candidate evaluation) because {obstacle}; choose a run environment "
                    "that can, such as docker, slurm or modal"
                ),
            )
        )
