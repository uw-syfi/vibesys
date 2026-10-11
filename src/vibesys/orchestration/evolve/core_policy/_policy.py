"""The evolve core policy: every pure input the host passes the core for an evolve run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, Field

from vibesys.orchestration.evolve.core_policy._limits import limits_for, run_deadline_at
from vibesys.orchestration.evolve.core_policy._replies import reply_schemas
from vibesys.orchestration.evolve.strategy.api import (
    JUDGE_REPLY,
    EvolveConfig,
    EvolveStrategy,
    Role,
    role_id,
)
from vibesys.orchestration.profilers import ProfilerKind, profiler_definition
from vs_core.api import (
    ArtifactRef,
    AssessmentAuthority,
    AssessmentKind,
    EvidenceRequirements,
    RevisionRef,
    WorkspaceMode,
)
from vs_core.api import RunFacts as CoreRunFacts

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vibesys.orchestration.evolve.models import EvolveOptions
    from vs_core.api import Limits, SchemaRef

type Seconds = Annotated[int, Field(gt=0)]


class _Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RunBounds(_Value):
    """Time bounds and pacing from the validated product config (`[evaluation]` and `[run]`)."""

    queue_allowance_seconds: Seconds
    observe_interval_seconds: Seconds
    observe_backoff_cap_seconds: Seconds
    # None means the run has no wall-clock deadline.
    max_run_seconds: Seconds | None = None


class PolicyInputs(_Value):
    """What the host resolved about the run before the strategy starts, as plain values."""

    objective: Annotated[str, Field(min_length=1)]
    baseline_commit: Annotated[str, Field(min_length=1)]
    evaluator_digest: Annotated[str, Field(min_length=1)]
    workload_digest: Annotated[str, Field(min_length=1)]
    environment_digest: Annotated[str, Field(min_length=1)]
    recipe: ArtifactRef
    accuracy_configured: bool
    benchmark_configured: bool
    isolated_candidates: bool
    domain_id: str = "generic"
    profiler_id: str = "none"
    profile_execution: str = "local"
    benchmark_command: str | None = None
    reference_path: str = "."
    accuracy_seconds: Annotated[float, Field(gt=0)] | None = None
    benchmark_seconds: Annotated[float, Field(gt=0)] | None = None


@dataclass(frozen=True)
class EvolveCorePolicy:
    """What the shell needs to start, run and resume an evolve search on the core."""

    strategy: EvolveStrategy
    facts: CoreRunFacts
    limits: Limits
    deadline_at: float
    reply_schemas: Mapping[SchemaRef, type[BaseModel]]
    requirements: EvidenceRequirements


def requirements_for(config: EvolveConfig) -> EvidenceRequirements:
    """The judge vouches for local validation; each configured gate must be satisfied.

    Core grants no role implicit authority, so without the judge's declared authority its
    verdict is not a valid assessment and no individual is ever winner-eligible.
    """
    return EvidenceRequirements(
        assessment_authorities=(
            AssessmentAuthority(
                kind=AssessmentKind.LOCAL_VALIDATION,
                role_id=role_id(Role.JUDGE.value),
                output_schema=JUDGE_REPLY,
            ),
        ),
        required_assessments=(
            *((AssessmentKind.CORRECTNESS,) if config.accuracy_configured else ()),
            *((AssessmentKind.BENCHMARK,) if config.benchmark_configured else ()),
        ),
    )


def _modality(options: EvolveOptions, inputs: PolicyInputs) -> str | None:
    """The modality the prompts adapt to: the option, else text generation for LLM serving."""
    if options.modality is not None:
        return options.modality
    return "text_generation" if inputs.domain_id == "llm-serving" else None


def config_for(options: EvolveOptions, bounds: RunBounds, inputs: PolicyInputs) -> EvolveConfig:
    """Project the validated options and resolved inputs onto the strategy's static config."""
    stage_budgets = {
        name: float(seconds)
        for name, seconds in (
            ("accuracy_seconds", inputs.accuracy_seconds),
            ("benchmark_seconds", inputs.benchmark_seconds),
        )
        if seconds is not None
    }
    profiler = (
        {}
        if inputs.profiler_id == "none"
        else {
            "profiler_support_name": profiler_definition(
                ProfilerKind(inputs.profiler_id)
            ).support_name,
            "profiler_mcp_name": profiler_definition(ProfilerKind(inputs.profiler_id)).mcp_name,
        }
    )
    openevolve = options.openevolve_config()
    return EvolveConfig.model_validate(
        {
            "recipe": inputs.recipe,
            "max_generations": options.max_generations,
            "children_per_generation": options.children_per_generation,
            "k_top_inspirations": options.k_top_inspirations,
            "k_random_inspirations": options.k_random_inspirations,
            "selection_temperature": options.selection_temperature,
            "seed": options.seed,
            "selector": options.search_policy
            or ("openevolve" if openevolve is not None else "vibesys"),
            "openevolve": openevolve,
            "frontier_bias": options.frontier_bias,
            "bootstrap_max_attempts": options.bootstrap_max_attempts,
            "metric_space": options.metric_space,
            "turn_drop_backoff_seconds": options.turn_drop_backoff_seconds,
            "profiler_id": inputs.profiler_id,
            "profile_execution": inputs.profile_execution,
            "benchmark_command": inputs.benchmark_command,
            "modality": _modality(options, inputs),
            "reference_path": inputs.reference_path,
            "benchmark_configured": inputs.benchmark_configured,
            "accuracy_configured": inputs.accuracy_configured,
            "queue_allowance_seconds": float(bounds.queue_allowance_seconds),
            # Concurrent children need isolated candidate workspaces; the exclusive root runs one.
            "max_parallelism": options.max_parallelism if inputs.isolated_candidates else 1,
            "workspace_mode": (
                WorkspaceMode.ISOLATED_CHILD
                if inputs.isolated_candidates
                else WorkspaceMode.EXCLUSIVE_ROOT
            ),
            **profiler,
            **stage_budgets,
        }
    )


def build_core_policy(
    options: EvolveOptions, bounds: RunBounds, inputs: PolicyInputs
) -> EvolveCorePolicy:
    """The policy of one evolve run from its validated options and resolved inputs."""
    config = config_for(options, bounds, inputs)
    return EvolveCorePolicy(
        strategy=EvolveStrategy(config=config),
        facts=CoreRunFacts(
            objective=inputs.objective,
            baseline=RevisionRef.of_git_commit(inputs.baseline_commit),
            evaluator_digest=inputs.evaluator_digest,
            workload_digest=inputs.workload_digest,
            environment_digest=inputs.environment_digest,
        ),
        limits=limits_for(
            config,
            observe_interval=float(bounds.observe_interval_seconds),
            observe_backoff_cap=float(bounds.observe_backoff_cap_seconds),
        ),
        deadline_at=run_deadline_at(bounds.max_run_seconds),
        reply_schemas=reply_schemas(),
        requirements=requirements_for(config),
    )
