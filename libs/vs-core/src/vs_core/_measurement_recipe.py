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

from vs_core.types.common import (
    ArtifactRef,
    EvidenceKind,
    RevisionRef,
    RunFacts,
    Value,
    unset,
)
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
    accuracy_gated: tuple[EvidenceKind, ...]
    """Evidence kinds that count only if the accuracy stage passed; the task's trust rule."""
    dedicated_measurement_resources: bool = Field(default=False, exclude_if=unset)
    """The run's executor gives each job its resources alone (``RunFacts``)."""

    @classmethod
    def of(cls, facts: RunFacts) -> MeasurementRecipe:
        """The recipe the run's task fixes: its terms with the run's evaluation identities."""
        task = facts.task
        if task is None:
            message = "the run carries no task terms: the host must resolve them"
            raise ValueError(message)
        return cls(
            evaluator_digest=facts.evaluator_digest,
            workload_digest=facts.workload_digest,
            environment_digest=facts.environment_digest,
            recipe=task.recipe,
            stages=tuple(
                StageBudget(stage_id=item.stage_id, execution_seconds=item.execution_seconds)
                for item in task.stages
            ),
            queue_allowance=task.queue_allowance,
            accuracy_stage=task.accuracy_stage,
            accuracy_gated=task.accuracy_gated,
            dedicated_measurement_resources=facts.dedicated_measurement_resources,
        )

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

        The accuracy gate, and the gating that needs it, are dropped with its stage.
        """
        kept = tuple(item for item in self.stages if item.stage_id in names)
        gate = (
            self.accuracy_stage
            if any(item.stage_id == self.accuracy_stage for item in kept)
            else None
        )
        return self.model_copy(
            update={
                "stages": kept,
                "accuracy_stage": gate,
                "accuracy_gated": self.accuracy_gated if gate is not None else (),
            }
        )

    def plan(
        self,
        candidate: RevisionRef,
        *,
        purpose: MeasurementPurpose,
        now_at: float,
        stages: tuple[str, ...] | None = None,
    ) -> MeasurementPlan:
        """The ordered plan over ``stages`` (default: all) for this candidate, submitted now.

        Each stage depends on the one before it. A plan without the accuracy stage carries
        no accuracy gating.
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
            accuracy_gated=self.accuracy_gated if self.accuracy_stage in names else (),
            policy="ordered",
            recipe=self.recipe,
            submitted_at=now_at,
            queue_allowance=self.queue_allowance,
            dedicated_measurement_resources=self.dedicated_measurement_resources,
        )

    def budget(self, stages: tuple[str, ...] | None = None) -> float:
        """The most time measuring ``stages`` (default: all) can take, queue allowance included.

        What a wait for the plan states as its limit: ``Within(recipe.budget())``.
        """
        seconds = {item.stage_id: item.execution_seconds for item in self.stages}
        names = tuple(seconds) if stages is None else stages
        return self.queue_allowance + sum(seconds[name] for name in names)
