"""The single core policy: every pure input the host passes the core for a single run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, Field

from vibesys.orchestration.single.core_policy._limits import limits_for, run_deadline_at
from vibesys.orchestration.single.core_policy._replies import reply_schemas
from vibesys.orchestration.single.strategy.api import (
    AttributionConfig,
    SingleConfig,
    SingleStrategy,
)
from vs_core.api import (
    ArtifactRef,
    AssessmentKind,
    EvidenceRequirements,
    RevisionRef,
    WorkspaceMode,
)
from vs_core.api import RunFacts as CoreRunFacts

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vibesys.orchestration.single.models import ProfileGuidedSingleOptions, SingleOptions
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
    profiler_id: str = "none"
    accuracy_seconds: Annotated[float, Field(gt=0)] | None = None
    benchmark_seconds: Annotated[float, Field(gt=0)] | None = None


@dataclass(frozen=True)
class SingleCorePolicy:
    """What the shell needs to start, run and resume a single search on the core."""

    strategy: SingleStrategy
    facts: CoreRunFacts
    limits: Limits
    deadline_at: float
    reply_schemas: Mapping[SchemaRef, type[BaseModel]]
    requirements: EvidenceRequirements


def requirements_for(config: SingleConfig) -> EvidenceRequirements:
    """Each configured gate must be satisfied; single declares no local-validation authority.

    The combined agent's self-review gates its own retries but is not independent evidence, so
    eligibility rests on the trusted gates alone.
    """
    return EvidenceRequirements(
        required_assessments=(
            *((AssessmentKind.CORRECTNESS,) if config.accuracy_configured else ()),
            *((AssessmentKind.BENCHMARK,) if config.benchmark_configured else ()),
        )
    )


def config_for(
    options: SingleOptions | ProfileGuidedSingleOptions, bounds: RunBounds, inputs: PolicyInputs
) -> SingleConfig:
    """Project the validated options and resolved inputs onto the strategy's static config."""
    stage_budgets = {
        name: float(seconds)
        for name, seconds in (
            ("accuracy_seconds", inputs.accuracy_seconds),
            ("benchmark_seconds", inputs.benchmark_seconds),
        )
        if seconds is not None
    }
    guided = options.profile_guided
    return SingleConfig.model_validate(
        {
            "recipe": inputs.recipe,
            "max_rounds": options.max_rounds,
            "max_retries_per_round": options.max_retries_per_round,
            "official_eval_every": options.official_eval_every,
            "judge_every": options.judge_every,
            "metric_space": options.metric_space,
            "turn_drop_backoff_seconds": options.turn_drop_backoff_seconds,
            "profiler_id": inputs.profiler_id,
            "interface": options.interface,
            "benchmark_configured": inputs.benchmark_configured,
            "accuracy_configured": inputs.accuracy_configured,
            "queue_allowance_seconds": float(bounds.queue_allowance_seconds),
            "workspace_mode": (
                WorkspaceMode.ISOLATED_CHILD
                if inputs.isolated_candidates
                else WorkspaceMode.EXCLUSIVE_ROOT
            ),
            "attribution": (
                None
                if guided is None
                else AttributionConfig(
                    command=guided.command,
                    timeout_seconds=guided.timeout_seconds,
                    min_measured_rounds=guided.min_measured_rounds,
                    min_relative_improvement=guided.min_relative_improvement,
                )
            ),
            **stage_budgets,
        }
    )


def build_core_policy(
    options: SingleOptions | ProfileGuidedSingleOptions, bounds: RunBounds, inputs: PolicyInputs
) -> SingleCorePolicy:
    """The policy of one single run from its validated options and resolved inputs."""
    config = config_for(options, bounds, inputs)
    return SingleCorePolicy(
        strategy=SingleStrategy(config=config),
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
