"""How the run measures a candidate: the single source of every measurement plan.

The strategy's trusted plan (the accuracy gate on the final candidate, which adoption requires)
and the plan an agent measures with through its in-turn tool are both built from this value, so
an agent measurement and a trusted one share one identity.
"""

from vibesys.orchestration.issue_queue.strategy._config import IssueQueueConfig
from vs_core.api import MeasurementPlan, MeasurementRecipe, RevisionRef, RunFacts, StageBudget


def measurement_recipe(config: IssueQueueConfig, facts: RunFacts) -> MeasurementRecipe:
    """How this run measures a candidate."""
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


def verification_plan(
    config: IssueQueueConfig, facts: RunFacts, revision: RevisionRef, now_at: float
) -> MeasurementPlan:
    """The accuracy gate on the revision the run would adopt."""
    return measurement_recipe(config, facts).plan(
        revision,
        purpose="official",
        now_at=now_at,
        stages=("accuracy",),
        submission_limit=config.max_measurement_submissions,
    )
