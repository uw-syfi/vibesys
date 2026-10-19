"""How the hypothesis records change as plans are scheduled and workstreams finish: pure values."""

from vibesys.orchestration.dynamic.search._outcomes import WorkstreamDone, round_of
from vibesys.orchestration.dynamic.search._plan import PlanUpdate
from vibesys.orchestration.dynamic.search._values import (
    CombineSource,
    HypothesisRecord,
    WorkPlan,
)


def scheduled(
    known: HypothesisRecord | None,
    plan: WorkPlan,
    sequence: int,
    parts: tuple[CombineSource, ...] = (),
) -> HypothesisRecord:
    """The record of the hypothesis a plan to implement works on, new or with its latest task.

    A new hypothesis a combine starts keeps its ``parts``, so its rows show them for good.
    """
    if known is not None:
        return known.model_copy(update={"last_task": plan.task})
    return HypothesisRecord(
        hypothesis_id=plan.work_id,
        title=plan.title,
        hypothesis=plan.hypothesis,
        first_sequence=sequence,
        lineage_parent_id=plan.parent_hypothesis_id,
        last_task=plan.task,
        combined_from=parts,
    )


def with_round(current: HypothesisRecord, done: WorkstreamDone) -> HypothesisRecord:
    """The record with the round the finished implement workstream added."""
    continued = done.start.plan.continue_hypothesis
    return current.model_copy(
        update={
            "rounds": (*current.rounds, round_of(done)),
            "continuation_rounds": current.continuation_rounds + (1 if continued else 0),
        }
    )


def with_disposition(current: HypothesisRecord, change: PlanUpdate) -> HypothesisRecord:
    """The record after the planner parked or abandoned the hypothesis."""
    return current.model_copy(
        update={
            "strategy": change.disposition,
            "reason": change.reason,
            "reason_kind": change.reason_kind,
        }
    )
