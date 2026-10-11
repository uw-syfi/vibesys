"""The issue-queue core policy: every pure input the host passes the core for a run."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, Field

from vibesys.orchestration.issue_queue.core_policy._limits import limits_for, run_deadline_at
from vibesys.orchestration.issue_queue.models import (
    ImplementerReplyModel,
    JudgeReplyModel,
    perf_result_model,
)
from vibesys.orchestration.issue_queue.strategy.api import (
    IMPLEMENTER_REPLY,
    JUDGE_REPLY,
    PERF_REPLY,
    IssueQueueConfig,
    IssueQueueStrategy,
    Role,
    role_id,
)
from vs_core.api import (
    ArtifactRef,
    AssessmentAuthority,
    AssessmentKind,
    EvidenceRequirements,
    RevisionRef,
)
from vs_core.api import RunFacts as CoreRunFacts

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vibesys.orchestration.issue_queue.models import IssueQueueOptions
    from vs_core.api import Limits, SchemaRef

type Seconds = Annotated[int, Field(gt=0)]


class _Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RunBounds(_Value):
    """Time bounds and pacing from the validated product config (`[evaluation]` and `[run]`)."""

    queue_allowance_seconds: Seconds
    # How often core polls a running measurement job, and the longest delay after a poll that
    # could not read the job.
    observe_interval_seconds: Seconds
    observe_backoff_cap_seconds: Seconds
    # None means the run has no wall-clock deadline.
    max_run_seconds: Seconds | None = None


class PolicyInputs(_Value):
    """What the host resolved about the run before the strategy starts, as plain values.

    The digests are the run's evaluator, workload and environment identities as the evaluation
    backend computes them. `recipe` names the stored evaluation plan every measurement plan
    cites. A stage budget of None means the plan declares no timeout for it.
    """

    objective: Annotated[str, Field(min_length=1)]
    baseline_commit: Annotated[str, Field(min_length=1)]
    evaluator_digest: Annotated[str, Field(min_length=1)]
    workload_digest: Annotated[str, Field(min_length=1)]
    environment_digest: Annotated[str, Field(min_length=1)]
    recipe: ArtifactRef
    accuracy_configured: bool
    benchmark_configured: bool
    accuracy_seconds: Seconds | None = None
    benchmark_seconds: Seconds | None = None
    profile_seconds: Seconds | None = None


@dataclass(frozen=True)
class IssueQueueCorePolicy:
    """What the shell needs to start, run and resume an issue-queue run on the core.

    `facts`, `limits` and `deadline_at` go into the core state; `reply_schemas` into the
    session resolver; `strategy.declaration` names the operations to offer.
    """

    strategy: IssueQueueStrategy
    facts: CoreRunFacts
    limits: Limits
    deadline_at: float
    reply_schemas: Mapping[SchemaRef, type[BaseModel]]
    requirements: EvidenceRequirements


def requirements_for() -> EvidenceRequirements:
    """The judge vouches for local validation, and a closed issue needs that vouching.

    Core grants no role implicit authority, so without the judge's declared authority its
    verdict is not a valid assessment and no candidate is ever winner-eligible. The loop has
    no trusted gates of its own, so the judge's assessment is the only one required.
    """
    return EvidenceRequirements(
        assessment_authorities=(
            AssessmentAuthority(
                kind=AssessmentKind.LOCAL_VALIDATION,
                role_id=role_id(Role.JUDGE.value),
                output_schema=JUDGE_REPLY,
            ),
        ),
        required_assessments=(AssessmentKind.LOCAL_VALIDATION,),
    )


def reply_schemas(config: IssueQueueConfig) -> Mapping[SchemaRef, type[BaseModel]]:
    """The reply type of every schema the strategy asks a role to answer with.

    The judge's schema admits one filed bug of type `bug`; the evaluator's admits at most
    the configured number of issues, so a reply beyond either is not a valid reply.
    """
    return {
        IMPLEMENTER_REPLY: ImplementerReplyModel,
        JUDGE_REPLY: JudgeReplyModel,
        PERF_REPLY: perf_result_model(config.max_issues_per_perf_eval),
    }


def config_from(
    options: IssueQueueOptions, inputs: PolicyInputs, bounds: RunBounds
) -> IssueQueueConfig:
    """Project the validated plugin options onto the strategy's static config."""
    load_levels = (
        None
        if options.load_levels is None
        else json.dumps(
            [level.model_dump(mode="json") for level in options.load_levels], sort_keys=True
        )
    )
    stage_budgets = {
        name: float(seconds)
        for name, seconds in (
            ("accuracy_seconds", inputs.accuracy_seconds),
            ("benchmark_seconds", inputs.benchmark_seconds),
            ("profile_seconds", inputs.profile_seconds),
        )
        if seconds is not None
    }
    return IssueQueueConfig(
        recipe=inputs.recipe,
        accuracy_configured=inputs.accuracy_configured,
        benchmark_configured=inputs.benchmark_configured,
        queue_allowance_seconds=float(bounds.queue_allowance_seconds),
        **stage_budgets,
        max_rounds=options.max_rounds,
        max_attempts_per_issue=options.max_attempts_per_issue,
        max_issues_per_perf_eval=options.max_issues_per_perf_eval,
        max_issues_per_round=options.issues_per_round,
        turn_drop_backoff_seconds=options.turn_drop_backoff_seconds,
        load_levels_json=load_levels,
    )


def build_core_policy(
    options: IssueQueueOptions, bounds: RunBounds, inputs: PolicyInputs
) -> IssueQueueCorePolicy:
    """The policy of one issue-queue run from its validated options and resolved inputs."""
    config = config_from(options, inputs, bounds)
    return IssueQueueCorePolicy(
        strategy=IssueQueueStrategy(config=config),
        facts=CoreRunFacts(
            objective=inputs.objective,
            baseline=RevisionRef.of_git_commit(inputs.baseline_commit),
            evaluator_digest=inputs.evaluator_digest,
            workload_digest=inputs.workload_digest,
            environment_digest=inputs.environment_digest,
        ),
        limits=limits_for(
            config,
            queue_allowance=float(bounds.queue_allowance_seconds),
            observe_interval=float(bounds.observe_interval_seconds),
            observe_backoff_cap=float(bounds.observe_backoff_cap_seconds),
        ),
        deadline_at=run_deadline_at(bounds.max_run_seconds),
        reply_schemas=reply_schemas(config),
        requirements=requirements_for(),
    )
