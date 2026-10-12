"""The one statement of how a run measures a candidate.

A trusted measurement (the strategy's own `Measure` decision) and an agent's tool measurement
(the host bridge admitting an event on the agent's behalf) must share one identity, or the
evidence of one cannot stand for the other. That holds by construction only if both build their
`MeasurementPlan` from one value: the evaluator, workload and environment digests, the recipe
artifact, the stages in dependency order with their execution budgets, and the queue
allowance. This module is that value; nothing else assembles a plan.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from vs_core.types.common import ArtifactRef, RevisionRef, Value
from vs_core.types.evaluation import MeasurementPlan, MeasurementStage, StageRequirements

type MeasurementPurpose = Literal["baseline", "local-validation", "official", "profile"]


class StageBudget(StageRequirements):
    """One measurable stage and the seconds its execution may take."""

    stage_id: str = Field(min_length=1)
    execution_seconds: float = Field(ge=0)


class MeasurementRecipe(Value):
    """What the host fixes about a measurement; the strategy and the agent supply none of it."""

    evaluator_digest: str
    workload_digest: str
    environment_digest: str
    recipe: ArtifactRef
    stages: tuple[StageBudget, ...]
    """Every stage the run can measure, in dependency order."""
    queue_allowance: float = Field(ge=0)
    accuracy_stage: str | None = Field(default=None, min_length=1)
    """The stage whose pass is the correctness gate, used by plans that include it."""

    def requiring(self, stage_id: str, outputs: tuple[str, ...]) -> MeasurementRecipe:
        """The same recipe whose ``stage_id`` evidence must contain ``outputs``."""
        if all(item.stage_id != stage_id for item in self.stages):
            message = f"cannot require outputs of unknown stage {stage_id!r}"
            raise ValueError(message)
        return self.model_copy(
            update={
                "stages": tuple(
                    item.model_copy(update={"required_outputs": outputs})
                    if item.stage_id == stage_id
                    else item
                    for item in self.stages
                )
            }
        )

    def only(self, names: tuple[str, ...]) -> MeasurementRecipe:
        """The same recipe over the named stages, keeping the recipe's own order.

        The accuracy gate is dropped with its stage.
        """
        kept = tuple(item for item in self.stages if item.stage_id in names)
        gate = (
            self.accuracy_stage
            if any(item.stage_id == self.accuracy_stage for item in kept)
            else None
        )
        return self.model_copy(update={"stages": kept, "accuracy_stage": gate})

    def plan(
        self,
        candidate: RevisionRef,
        *,
        purpose: MeasurementPurpose,
        now_at: float,
        stages: tuple[str, ...] | None = None,
        submission_limit: int = 1,
    ) -> MeasurementPlan:
        """The ordered plan over ``stages`` (default: all) for this candidate, submitted now.

        Each stage depends on the one before it. ``submission_limit`` bounds how often core
        resubmits after an infrastructure failure.
        """
        budgets = {item.stage_id: item.execution_seconds for item in self.stages}
        outputs = {item.stage_id: item.required_outputs for item in self.stages}
        names = tuple(budgets) if stages is None else stages
        ordered = tuple(
            MeasurementStage(
                stage_id=name,
                depends_on=names[index - 1 : index] if index else (),
                execution_budget=budgets[name],
                required_outputs=tuple(sorted(set(outputs.get(name, ())))),
            )
            for index, name in enumerate(names)
        )
        return MeasurementPlan(
            purpose=purpose,
            candidate=candidate,
            evaluator_digest=self.evaluator_digest,
            workload_digest=self.workload_digest,
            environment_digest=self.environment_digest,
            stages=ordered,
            accuracy_stage=self.accuracy_stage if self.accuracy_stage in names else None,
            policy="ordered",
            recipe=self.recipe,
            submission_limit=submission_limit,
            submitted_at=now_at,
            queue_allowance=self.queue_allowance,
            deadline_at=now_at
            + sum(item.execution_budget for item in ordered)
            + self.queue_allowance,
        )
