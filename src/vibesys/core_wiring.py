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

from vibesys.constants import DomainName
from vibesys.domains.registry import resolve_domain
from vibesys.domains.rendering import render_domain_sections
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.run.evaluation_identity import semantic_evaluation_identity
from vs_core.api import ArtifactId, ArtifactRef, EvidenceKind
from vs_runtime.api.core import (
    AgentEvaluationPolicy,
    OperationCatalog,
    build_operation_catalog,
    operation_roles,
    production_owners,
)
from vs_runtime.api.infrastructure import ParallelCandidateObstacle

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vs_core.api import MeasurementRecipe, StrategyOperations
    from vs_runtime.api import ArtifactStore, ProfileField, RunFacts
    from vs_runtime.api.core import OperationPorts
    from vs_runtime.api.infrastructure import RunEnvironmentView, TrustedEvaluationPlan

__all__ = [
    "EvaluationInputs",
    "ParallelCandidateFacts",
    "ResolvedRun",
    "agent_evaluation_policy",
    "domain_prompt_sections",
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
    def parallel_candidate_obstacle(self) -> ParallelCandidateObstacle | None:
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
    profile_capture_withheld: bool
    """A strategy that offers profile work was denied it: the plan names a profile command and a
    profiler is chosen, but the run environment has no framework-side capture."""
    profile_fields: tuple[ProfileField, ...]
    build_seconds: float | None
    accuracy_seconds: float | None
    benchmark_seconds: float | None
    profile_seconds: float | None
    benchmark_needs_accuracy: bool = True
    """The task requires a benchmark to pass the accuracy gate (``requires_accuracy``)."""
    builds: bool = False
    """The task declares a candidate build, measured before its other gates."""
    dedicated_measurement_resources: bool = False
    """The trusted executor runs each measurement on hosts it holds alone."""


def evaluation_inputs(run: ResolvedRun) -> EvaluationInputs:
    """Store the evaluation recipe and read the run's evaluation identity and budgets."""
    plan = run.evaluation_plan
    receipt = run.artifacts.write(plan.model_dump_json().encode())
    identity = semantic_evaluation_identity(plan, run.facts, run.environment)
    profiling = run.facts.profiler_id != "none"
    wants_profile_work = profiling and plan.profile_command is not None
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
        profile_measurement=wants_profile_work and run.environment.captures_profile,
        profile_capture_withheld=wants_profile_work and not run.environment.captures_profile,
        profile_fields=run.environment.supported_profile_fields,
        build_seconds=None if plan.build is None else plan.build.timeout_seconds,
        accuracy_seconds=plan.accuracy_timeout_seconds,
        benchmark_seconds=plan.benchmark_timeout_seconds,
        profile_seconds=plan.profile_timeout_seconds,
        benchmark_needs_accuracy=EvidenceKind.BENCHMARK in plan.accuracy_gated,
        builds=plan.build is not None,
        dedicated_measurement_resources=run.environment.dedicated_measurement_resources,
    )


def agent_evaluation_policy(
    recipe: MeasurementRecipe, *, accuracy: bool, benchmark: bool
) -> AgentEvaluationPolicy:
    """The plan an agent measures with: the run's configured gates, once each.

    It is built from the same recipe as the strategy's own trusted measurement plans, so an
    agent measurement's identity equals the strategy's official one in everything but the
    purpose (``local-validation``). Core lets an official measurement reuse the scope's one
    agent run of the same measured input (``vs_core``'s ``_measurement_reuse``). A task's build
    stage, when the recipe has one, comes first: the gates after it load what it built.
    """
    builds = any(item.stage_id == "build" for item in recipe.stages)
    configured = tuple(
        name
        for name, enabled in (
            ("build", builds and (accuracy or benchmark)),
            ("accuracy", accuracy),
            ("benchmark", benchmark),
        )
        if enabled
    )
    return AgentEvaluationPolicy.from_recipe(recipe.only(configured))


def operation_catalog(operations: StrategyOperations, ports: OperationPorts) -> OperationCatalog:
    """The operations a strategy requires, each owned by the production owner of its role."""
    registrations = operation_roles(operations)
    return build_operation_catalog(registrations, production_owners(registrations, ports))


_OBSTACLE_WORDING: Mapping[ParallelCandidateObstacle, str] = {
    ParallelCandidateObstacle.NO_OWN_CONTAINER: "its agent does not run in a container of its own",
    ParallelCandidateObstacle.BORROWED_SESSION: (
        "its agent clients borrow the run-owned sandbox and bridge"
    ),
}


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
                    f"candidate evaluation) because {_OBSTACLE_WORDING[obstacle]}; choose a run environment "
                    "that can, such as docker, slurm or modal"
                ),
            )
        )


def domain_prompt_sections(
    facts: RunFacts, *, modality: str | None = None, interface: str = "inprocess"
) -> Mapping[str, str]:
    """The run's domain pack as one rendered section per role, for the plan's prompt variables.

    Every strategy's role prompts render ``domain_sections[<role>]`` in the same slot
    (`core_protocol/domain_section.j2`), so a domain's contract text reaches every strategy
    through this one path. The pack is addressed only by the run's domain; a role the pack
    leaves out renders as an empty section.
    """
    domain = resolve_domain(DomainName(facts.domain_id))
    modality = modality or domain.default_modality
    if modality is not None and modality not in domain.modalities:
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="modality_not_in_domain",
                stage="prompt_validation",
                message=(
                    f"modality {modality!r} is not provided by the {domain.name.value!r} domain; "
                    f"it provides: {', '.join(domain.modalities) or 'none'}"
                ),
            )
        )
    sections = render_domain_sections(
        domain,
        modality=modality,
        interface=interface,
        reference_path=facts.reference_location,
        benchmark_command=facts.benchmark_command,
        accuracy_command=facts.accuracy_command,
        runtime_notes=facts.environment_notes,
        profile_execution=facts.profile_execution.value,
        profiler_id=facts.profiler_id,
        workspace_sources=facts.workspace_sources,
    )
    return {role.value: text for role, text in sections.items()}
