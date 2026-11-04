"""A task's implementation plan: the design documents and the ordered steps a plan-mode run follows.

A task that arrives with a worked-out design declares it in the ``[plan]`` table of its input
manifest. The design itself stays in task files the agents read (``documents``); the table holds
what code acts on: each step's id, the documents it implements, when it is done, whether reaching
it warrants a trusted evaluation, and which steps it builds on. The steps are optional: without
them, a plan-mode run asks its planner to outline them from the documents, and checks that
outline with the same rules (`outline_problems`). Validation is twofold: the table's shape and
its step graph when the manifest is parsed, and that every document exists inside the task when
the task is loaded (`check_plan_documents`). Every error names the key path it is about.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from vs_project.api import ProjectLayoutError

if TYPE_CHECKING:
    from vs_project.api import TaskDirectory

MAX_STEPS = 64
"""Steps one plan may declare: a design broken into more is a plan for several runs."""


def _task_relative(value: str) -> str:
    path = PurePosixPath(value)
    if not value.strip() or path.is_absolute():
        message = f"{value!r} must be a path relative to the task directory"
        raise ValueError(message)
    if any(part in {"", ".", ".."} for part in value.split("/")):
        message = f"{value!r} must not contain empty, current, or parent path components"
        raise ValueError(message)
    return value


type TaskPath = Annotated[str, AfterValidator(_task_relative)]
"""A task-relative file path; that it exists inside the task is checked when the task loads."""

type StepId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")]
"""A step's id: lowercase letters, digits, ``_`` and ``-``; it names the step's workstreams."""


class StepEvaluation(StrEnum):
    """Whether reaching a step warrants a trusted evaluation."""

    NONE = "none"
    """The step is accepted on its own checks (the implementer's tests, the judge's review)."""
    TRUSTED = "trusted"
    """A milestone: the candidate that reaches it is measured by the task's trusted evaluation."""


class DeviationPolicy(StrEnum):
    """What an implementer may do where its work departs from the plan's documents."""

    RECORD = "record"
    """Depart only with a recorded reason, which the judge, the planner and later steps see."""
    FORBID = "forbid"
    """Never depart: a reply that records a deviation does not count as the step's candidate."""


class PlanStep(BaseModel):
    """One step of the plan: a unit of implementation the run schedules as a workstream."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: StepId
    title: str = Field(min_length=1, max_length=200)
    documents: tuple[TaskPath, ...] = ()
    """The parts of the design this step implements, beside the plan's own documents."""
    done_when: str = Field(min_length=1, max_length=2000)
    """When the step is done, as its implementer and the judge check it."""
    evaluate: StepEvaluation
    depends_on: tuple[StepId, ...] = ()
    """The steps it builds on; it starts once they are done."""


class TaskPlan(BaseModel):
    """The ``[plan]`` table: the design a plan-mode run follows."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    documents: tuple[TaskPath, ...] = Field(min_length=1)
    """The design, read by every agent of a plan-mode run."""
    deviations: DeviationPolicy = DeviationPolicy.RECORD
    steps: tuple[PlanStep, ...] = Field(default=(), max_length=MAX_STEPS)

    @model_validator(mode="after")
    def _steps_form_a_plan(self) -> TaskPlan:
        problems = outline_problems(self.steps)
        if problems:
            raise ValueError("; ".join(problems))
        return self


def outline_problems(steps: tuple[PlanStep, ...]) -> list[str]:
    """What keeps ``steps`` from being one plan, each naming its key path; empty when none.

    Ids are unique, every dependency names a declared step other than itself, and the
    dependencies have no cycle, so the steps have an order.
    """
    problems: list[str] = []
    seen: set[str] = set()
    for index, step in enumerate(steps):
        if step.id in seen:
            problems.append(f"steps[{index}].id: duplicate step id {step.id!r}")
        seen.add(step.id)
    for index, step in enumerate(steps):
        for position, needed in enumerate(step.depends_on):
            where = f"steps[{index}].depends_on[{position}]"
            if needed == step.id:
                problems.append(f"{where}: step {step.id!r} depends on itself")
            elif needed not in seen:
                problems.append(f"{where}: unknown step {needed!r}")
    if not problems and (stuck := _in_cycles(steps)):
        problems.append(f"steps: dependency cycle among {', '.join(map(repr, stuck))}")
    return problems


def _in_cycles(steps: tuple[PlanStep, ...]) -> list[str]:
    """The ids of steps that can never start because their dependencies form a cycle."""
    waiting = {step.id: set(step.depends_on) for step in steps}
    while ready := [id_ for id_, needs in waiting.items() if not needs]:
        for id_ in ready:
            del waiting[id_]
        for needs in waiting.values():
            needs.difference_update(ready)
    return [step.id for step in steps if step.id in waiting]


def check_plan_documents(task: TaskDirectory, plan: TaskPlan) -> None:
    """Fail unless every document the plan names is a file inside the task directory.

    Raises ``FileNotFoundError`` or ``ValueError`` naming the key path and the file.
    """
    named = [(f"plan.documents[{i}]", path) for i, path in enumerate(plan.documents)]
    named += [
        (f"plan.steps[{i}].documents[{j}]", path)
        for i, step in enumerate(plan.steps)
        for j, path in enumerate(step.documents)
    ]
    for where, path in named:
        try:
            resolved = task.resolve(path, must_exist=False)
        except ProjectLayoutError as error:
            message = f"{where}: {error}"
            raise ValueError(message) from error
        if not resolved.exists():
            message = f"{where}: {path!r} does not exist in the task directory"
            raise FileNotFoundError(message)
        if not resolved.is_file():
            message = f"{where}: {path!r} is not a file"
            raise ValueError(message)


def plan_toml_lines(plan: TaskPlan) -> list[str]:
    """The ``[plan]`` table as TOML lines, in the order the manifest renders its tables."""
    lines = [
        "",
        "[plan]",
        f"documents = {_array(plan.documents)}",
        f"deviations = {_string(plan.deviations.value)}",
    ]
    for step in plan.steps:
        lines += [
            "",
            "[[plan.steps]]",
            f"id = {_string(step.id)}",
            f"title = {_string(step.title)}",
            f"documents = {_array(step.documents)}",
            f"done_when = {_string(step.done_when)}",
            f"evaluate = {_string(step.evaluate.value)}",
            f"depends_on = {_array(step.depends_on)}",
        ]
    return lines


def _string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _array(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_string(value) for value in values) + "]"
