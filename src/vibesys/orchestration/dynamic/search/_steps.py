"""A plan-mode run's outline and the state of each step, as plain values and pure functions.

A run launched with ``mode = "follow_plan"`` follows the task's plan (the ``[plan]`` table of
its input, `vibesys.inputs.TaskPlan`) instead of exploring. `Outline` is the search's copy of
that table: the same fields, as a strict persisted value, validated by the table's own rules
(`TaskPlan`), so the two cannot accept different plans. The outline is the one source of truth
for step order; each step is scheduled as an implement workstream whose ``work_id`` is the
step's id, so a step's history is its hypothesis's rounds.

A step's state is derived, never stored: running while a workstream of it runs, else what its
latest workstream settled (`step_states`). A step is ready when every step it depends on is
done (`ready_steps`), and builds on the newest revision those steps settled (`built_on`).
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from pydantic import Field, model_validator

from vibesys.inputs import TaskPlan
from vibesys.orchestration.dynamic.search._values import (
    FinishedWork,
    HypothesisRecord,
    RoundRecord,
    Step,
    WorkKind,
    WorkPlan,
)
from vs_core.api.search import Value

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from vs_core.api.search import RevisionRef

type PlanMode = Literal["explore", "follow_plan"]
"""Whether a dynamic run explores hypotheses or follows the task's plan."""


class Outline(Value):
    """The task's plan as the search holds it: its documents, deviation policy and steps.

    ``steps`` is empty when the task declares none; a plan-mode run then asks for them first.
    """

    documents: tuple[str, ...] = Field(min_length=1)
    deviations: Literal["record", "forbid"] = "record"
    steps: tuple[Step, ...] = ()

    @model_validator(mode="after")
    def _is_a_task_plan(self) -> Outline:
        TaskPlan.model_validate(self.model_dump())
        return self

    def with_steps(self, steps: tuple[Step, ...]) -> Outline:
        """The same plan with ``steps``, checked by the same rules."""
        return Outline.model_validate(self.model_copy(update={"steps": steps}).model_dump())


class StepState(StrEnum):
    """Where a step of the outline stands."""

    PENDING = "pending"
    """Never started."""
    RUNNING = "running"
    CHECKED = "checked"
    """An ``evaluate = "none"`` step whose candidate the judge accepted; kept as work in progress."""
    EVALUATED = "evaluated"
    """An ``evaluate = "trusted"`` step whose candidate the trusted evaluation measured."""
    FAILED = "failed"
    """Its latest workstream settled without a candidate to build on."""


DONE = frozenset({StepState.CHECKED, StepState.EVALUATED})
"""The states a dependent step may build on."""


def step_plan(step: Step) -> WorkPlan:
    """The workstream that implements ``step`` for the first time."""
    return WorkPlan(
        kind=WorkKind.IMPLEMENT,
        work_id=step.id,
        title=step.title,
        hypothesis=step.title,
        task=step.title,
        pass_criteria=step.done_when,
        step=step,
    )


def _settled_round(
    step_id: str, finished: Sequence[FinishedWork], hypotheses: Mapping[str, HypothesisRecord]
) -> RoundRecord | None:
    """The round of the step's latest finished workstream, if it settled with a candidate."""
    latest = next((item for item in reversed(finished) if item.plan.work_id == step_id), None)
    record = hypotheses.get(step_id)
    if latest is None or record is None:
        return None
    found = next((row for row in record.rounds if row.sequence == latest.sequence), None)
    if found is None or found.candidate is None or found.failure is not None:
        return None
    return found


def step_states(
    steps: tuple[Step, ...],
    finished: Sequence[FinishedWork],
    hypotheses: Mapping[str, HypothesisRecord],
    running: Collection[str],
) -> dict[str, StepState]:
    """Each step's state, by id, from what the run recorded; ``running`` are work ids in flight."""
    states: dict[str, StepState] = {}
    for step in steps:
        if step.id in running:
            states[step.id] = StepState.RUNNING
        elif not any(item.plan.work_id == step.id for item in finished):
            states[step.id] = StepState.PENDING
        elif _settled_round(step.id, finished, hypotheses) is None:
            states[step.id] = StepState.FAILED
        elif step.evaluate == "none":
            states[step.id] = StepState.CHECKED
        else:
            states[step.id] = StepState.EVALUATED
    return states


def ready_steps(steps: tuple[Step, ...], states: Mapping[str, StepState]) -> tuple[Step, ...]:
    """The steps never started whose dependencies are all done, in declared order."""
    return tuple(
        step
        for step in steps
        if states[step.id] is StepState.PENDING
        and all(states[needed] in DONE for needed in step.depends_on)
    )


def built_on(
    step: Step,
    finished: Sequence[FinishedWork],
    hypotheses: Mapping[str, HypothesisRecord],
) -> tuple[RevisionRef, str] | None:
    """The revision a step starts from and the step that settled it; None for the input.

    That is the revision of the dependency whose settled workstream started last: on a chain
    of steps, the newest one, which holds all the work before it. Two dependencies on separate
    lines are not merged: the step builds on the later one only.
    """
    settled = [
        (row.sequence, row.candidate, needed)
        for needed in step.depends_on
        if (row := _settled_round(needed, finished, hypotheses)) is not None
        and row.candidate is not None
    ]
    if not settled:
        return None
    _, revision, needed = max(settled, key=lambda item: item[0])
    return revision, needed
