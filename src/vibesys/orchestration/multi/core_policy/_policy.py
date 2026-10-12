"""The multi core policy: every pure input the host passes the core for a multi run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, Field

from vibesys.orchestration.multi.core_policy._limits import limits_for, run_deadline_at
from vibesys.orchestration.multi.core_policy._replies import reply_schemas
from vibesys.orchestration.multi.strategy.api import (
    JUDGE_REPLY,
    AttributionConfig,
    MultiConfig,
    MultiStrategy,
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

    from vibesys.orchestration.multi.models import MultiOptions, ProfileGuidedMultiOptions
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
class MultiCorePolicy:
    """What the shell needs to start, run and resume a multi search on the core."""

    strategy: MultiStrategy
    facts: CoreRunFacts
    limits: Limits
    deadline_at: float
    reply_schemas: Mapping[SchemaRef, type[BaseModel]]
    requirements: EvidenceRequirements


def requirements_for(config: MultiConfig) -> EvidenceRequirements:
    """The judge vouches for local validation; each configured gate must be satisfied.

    Core grants no role implicit authority, so without the judge's declared authority its
    verdict is not a valid assessment and no candidate is ever winner-eligible.
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


def config_for(
    options: MultiOptions | ProfileGuidedMultiOptions, bounds: RunBounds, inputs: PolicyInputs
) -> MultiConfig:
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
    return MultiConfig.model_validate(
        {
            "recipe": inputs.recipe,
            "max_rounds": options.max_rounds,
            "max_retries_per_round": options.max_retries_per_round,
            "official_eval_every": options.official_eval_every,
            "judge_every": options.judge_every,
            "metric_space": options.metric_space,
            "turn_drop_backoff_seconds": options.turn_drop_backoff_seconds,
            "profiler_id": inputs.profiler_id,
            "profile_execution": inputs.profile_execution,
            "benchmark_command": inputs.benchmark_command,
            "modality": options.modality,
            "reference_path": inputs.reference_path,
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
            **profiler,
            **stage_budgets,
        }
    )


def build_core_policy(
    options: MultiOptions | ProfileGuidedMultiOptions, bounds: RunBounds, inputs: PolicyInputs
) -> MultiCorePolicy:
    """The policy of one multi run from its validated options and resolved inputs."""
    config = config_for(options, bounds, inputs)
    return MultiCorePolicy(
        strategy=MultiStrategy(config=config),
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
