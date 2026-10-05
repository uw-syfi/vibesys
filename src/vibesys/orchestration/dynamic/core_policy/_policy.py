"""The dynamic core policy: every pure input the host passes the core for a dynamic run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, Field

from vibesys.orchestration.dynamic.agents import JUDGE
from vibesys.orchestration.dynamic.core_policy._limits import limits_for, run_deadline_at
from vibesys.orchestration.dynamic.core_policy._replies import reply_schemas
from vibesys.orchestration.dynamic.strategy.api import JUDGE_REPLY, DynamicConfig, DynamicStrategy
from vs_core.api import (
    ArtifactRef,
    AssessmentAuthority,
    AssessmentKind,
    EvidenceRequirements,
    RevisionRef,
    RoleId,
)
from vs_core.api import RunFacts as CoreRunFacts

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vibesys.orchestration.dynamic.models import DynamicOptions
    from vs_core.api import Limits, SchemaRef

type Seconds = Annotated[int, Field(gt=0)]


class _Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RunBounds(_Value):
    """Time bounds and pacing from the validated product config (`[evaluation]` and `[run]`)."""

    queue_allowance_seconds: Seconds
    # How often core polls a running measurement job, and the longest delay after a
    # poll that could not read the job.
    observe_interval_seconds: Seconds
    observe_backoff_cap_seconds: Seconds
    # None means the run has no wall-clock deadline.
    max_run_seconds: Seconds | None = None


class PolicyInputs(_Value):
    """What the host resolved about the run before the strategy starts, as plain values.

    The digests are the run's evaluator, workload and environment identities as the
    evaluation backend computes them. `recipe` names the stored evaluation plan every
    measurement plan cites. A stage budget of None means the plan declares no timeout
    for it, and the strategy's own default applies.
    """

    objective: Annotated[str, Field(min_length=1)]
    baseline_commit: Annotated[str, Field(min_length=1)]
    evaluator_digest: Annotated[str, Field(min_length=1)]
    workload_digest: Annotated[str, Field(min_length=1)]
    environment_digest: Annotated[str, Field(min_length=1)]
    recipe: ArtifactRef
    accuracy_configured: bool
    benchmark_configured: bool
    profiling: bool
    profile_measurement: bool
    accuracy_seconds: Seconds | None = None
    benchmark_seconds: Seconds | None = None
    profile_seconds: Seconds | None = None


@dataclass(frozen=True)
class DynamicCorePolicy:
    """What the shell needs to start, run and resume a dynamic search on the core.

    `facts`, `limits` and `deadline_at` go into the core state; `reply_schemas` into
    the session resolver; `strategy.declaration` names the operations to offer.
    """

    strategy: DynamicStrategy
    facts: CoreRunFacts
    limits: Limits
    deadline_at: float
    reply_schemas: Mapping[SchemaRef, type[BaseModel]]
    requirements: EvidenceRequirements


def requirements_for(config: DynamicConfig) -> EvidenceRequirements:
    """The judge vouches for local validation; each configured gate must be satisfied.

    Core grants no role implicit authority, so without the judge's declared authority its
    verdict is not a valid assessment and no candidate is ever winner-eligible.
    """
    return EvidenceRequirements(
        assessment_authorities=(
            AssessmentAuthority(
                kind=AssessmentKind.LOCAL_VALIDATION,
                role_id=RoleId(root=JUDGE.id),
                output_schema=JUDGE_REPLY,
            ),
        ),
        required_assessments=(
            *((AssessmentKind.CORRECTNESS,) if config.accuracy_configured else ()),
            *((AssessmentKind.BENCHMARK,) if config.benchmark_configured else ()),
        ),
    )


def build_core_policy(
    options: DynamicOptions, bounds: RunBounds, inputs: PolicyInputs
) -> DynamicCorePolicy:
    """The policy of one dynamic run from its validated options and resolved inputs.

    The run's own facts override the strategy's defaults: which stages are
    configured, whether it profiles, the queue allowance and each declared stage
    budget.
    """
    stage_budgets = {
        name: float(seconds)
        for name, seconds in (
            ("accuracy_seconds", inputs.accuracy_seconds),
            ("benchmark_seconds", inputs.benchmark_seconds),
            ("profile_seconds", inputs.profile_seconds),
        )
        if seconds is not None
    }
    config = DynamicConfig.from_options(
        options,
        recipe=inputs.recipe,
        benchmark_configured=inputs.benchmark_configured,
        accuracy_configured=inputs.accuracy_configured,
        profiling=inputs.profiling,
        profile_measurement=inputs.profile_measurement,
        queue_allowance_seconds=float(bounds.queue_allowance_seconds),
        **stage_budgets,
    )
    return DynamicCorePolicy(
        strategy=DynamicStrategy(config=config),
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
        reply_schemas=reply_schemas(config),
        requirements=requirements_for(config),
    )
