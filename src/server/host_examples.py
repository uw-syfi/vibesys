"""Light contracts for the example catalog, the projects made from it or copied out, and a project's tasks.

``ExampleList`` is what ``vibesys example list --json`` prints and the helper's
``examples.list`` answers; ``CreatedProject`` is what ``project.create_from_example`` and
``project.copy_out`` answer. Pydantic and the standard library only, like
``server.host_api``, which embeds them in the helper's messages.

Public surface: ``ProjectLookupError``, ``ExampleSummary``, ``ExampleTask`` and what it holds, ``TaskGoal`` with
``bounded_goal`` and ``read_task_goal``, ``ExampleList``, ``CreatedProject``, and a project's tasks: ``TaskSummary``,
``TaskList``, ``TasksListRequest``, ``TasksListReply``, ``ProjectTaskGoalRequest``,
``ProjectTaskGoalReply``, and what a destination holds: ``ExampleDestination``,
``ExamplesDestinationRequest``, ``ExamplesDestinationReply``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from server.host_envelope import HelperErrorCode, HelperReplyBase, HelperRequestBase
from server.host_preflight import Backend
from vs_project.api import AcceleratorVendor, ExampleDomain, ModelMaker, load_objectives

if TYPE_CHECKING:
    from vs_project.api import TaskDirectory

GOAL_MAX_CHARS: Final = 32_000
"""The most goal text an ``ExampleTask`` carries; a longer ``OBJECTIVE.md`` is cut and flagged."""


class ProjectLookupError(Exception):
    """A project's tasks or runs could not be read; ``code`` says why and ``path`` where."""

    def __init__(self, code: HelperErrorCode, path: str, message: str) -> None:
        """Name the failure ``code`` for ``path``; ``message`` is for a person."""
        super().__init__(message)
        self.code = code
        self.path = path


class ExampleModel(BaseModel):
    """The model an LLM-serving task serves."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    """The display name, for example ``Llama 3.3 70B``."""
    maker: ModelMaker


class ExampleHardware(BaseModel):
    """The hardware a task targets."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    accelerator: str
    """The display name, for example ``NVIDIA H100`` or ``CPU``."""
    count: int
    vendor: AcceleratorVendor
    backend: Backend | None
    """The first compute backend the task declares; ``None`` when it declares none."""


class ExampleWorkload(BaseModel):
    """The workload a data-structure task drives."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str


class ExampleObjective(BaseModel):
    """One axis the task's ``objectives.toml`` declares."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric: str
    direction: Literal["max", "min"]


class TaskGoal(BaseModel):
    """What a task is for: its goal text and the axes it declares, read from its files."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task: str
    objectives: tuple[ExampleObjective, ...]
    """Empty when the task has no ``objectives.toml``."""
    goal: Annotated[str, Field(max_length=GOAL_MAX_CHARS)]
    """The task's ``OBJECTIVE.md``, cut to ``GOAL_MAX_CHARS``."""
    goal_truncated: bool


def bounded_goal(text: str) -> tuple[str, bool]:
    """``text`` cut to ``GOAL_MAX_CHARS``, and whether anything was cut."""
    return text[:GOAL_MAX_CHARS], len(text) > GOAL_MAX_CHARS


def read_task_goal(task: TaskDirectory) -> TaskGoal:
    """What ``task`` is for: its ``OBJECTIVE.md`` (cut to ``GOAL_MAX_CHARS``) and its axes.

    Raises ``OSError`` for a file that cannot be read and ``ValueError`` (naming the file) for
    ``objectives.toml`` that is invalid or a goal that is not UTF-8 text.
    """
    objectives = load_objectives(task.path).objective
    try:
        text = task.objective_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        message = f"{task.objective_path} is not UTF-8 text: {exc}"
        raise ValueError(message) from exc
    goal, truncated = bounded_goal(text)
    return TaskGoal(
        task=task.name.value,
        objectives=tuple(
            ExampleObjective(metric=axis.name, direction=axis.direction) for axis in objectives
        ),
        goal=goal,
        goal_truncated=truncated,
    )


class ExampleTask(TaskGoal):
    """What one task of an example is about, read from its ``vibesys.input.toml`` and goal file."""

    domain: ExampleDomain
    hardware: ExampleHardware
    model: ExampleModel | None
    """Present for ``llm-serving`` tasks."""
    workload: ExampleWorkload | None
    """Present for ``data-structures`` tasks."""


class ExampleSummary(BaseModel):
    """One example in ``vibesys example list --json``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    """The value ``vibesys example new`` accepts, e.g. ``data-structures/stack``."""
    category: str
    """The ``examples/<category>/`` directory the example is catalogued under."""
    tasks: tuple[str, ...]
    """Task names, sorted; empty for an external repository that is not fetched."""
    external_repo: bool
    """Whether ``example new`` clones another repository instead of copying files."""
    task_details: Annotated[
        tuple[ExampleTask, ...] | None, Field(exclude_if=lambda details: details is None)
    ] = None
    """What each task that declares a ``[catalogue]`` is about, sorted by task name. A task
    without the table is absent. Present only when the request set ``with_catalogue`` (feature
    ``example_catalogue``), so a strict client that did not ask never sees the field."""


class ExampleList(BaseModel):
    """``vibesys example list --json``: every catalogued example, by category then name."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    checkout: str
    """The resolved VibeSys checkout whose ``examples/`` the listing was read from."""
    examples: tuple[ExampleSummary, ...]


class CreatedProject(BaseModel):
    """A project made in its own new Git repository, and its tasks."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project: str
    """The new project's resolved directory."""
    tasks: tuple[str, ...]
    """Its task names, sorted."""


class TaskSummary(BaseModel):
    """One task of a project."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    """The task's directory name, the value ``--task`` accepts."""


class TaskList(BaseModel):
    """``vibesys tasks --json``: the tasks of one project."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    project_root: str
    """The resolved absolute project directory the tasks were read from."""
    tasks: tuple[TaskSummary, ...]


class TasksListRequest(HelperRequestBase):
    """The tasks a project defines under ``.vibesys/tasks/``."""

    type: Literal["tasks.list"] = "tasks.list"
    project: str
    """The project directory on the host; ``~`` expands to the helper's home."""


class TasksListReply(HelperReplyBase):
    """The project's tasks."""

    type: Literal["tasks.list"] = "tasks.list"
    tasks: TaskList


class ProjectTaskGoalRequest(HelperRequestBase):
    """The goal text and objectives of one task of a project."""

    type: Literal["project.task_goal"] = "project.task_goal"
    project: str
    """The project directory on the host; ``~`` expands to the helper's home."""
    task: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    """A name ``tasks.list`` listed."""


class ProjectTaskGoalReply(HelperReplyBase):
    """The task's goal and objectives."""

    type: Literal["project.task_goal"] = "project.task_goal"
    goal: TaskGoal


class ExampleDestination(StrEnum):
    """What is at the place an example would be copied to."""

    ABSENT = "absent"
    """Nothing: the copy creates it."""
    EMPTY = "empty"
    """An empty directory: the copy fills it, as for ``absent``."""
    EXAMPLE_PROJECT = "example_project"
    """A project with the example's tasks, which a run can use as it is."""
    OCCUPIED = "occupied"
    """Anything else: another project, files, or something that is not a directory."""


class ExamplesDestinationRequest(HelperRequestBase):
    """What ``project.create_from_example`` would find at ``destination`` for ``example``."""

    type: Literal["examples.destination"] = "examples.destination"
    example: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    """A name ``examples.list`` listed."""
    destination: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    """A path as typed, as for ``paths.complete``."""


class ExamplesDestinationReply(HelperReplyBase):
    """What the destination holds."""

    type: Literal["examples.destination"] = "examples.destination"
    state: ExampleDestination
    project: str | None
    """The resolved project directory for ``example_project``; ``None`` otherwise."""


__all__ = [
    "GOAL_MAX_CHARS",
    "CreatedProject",
    "ExampleDestination",
    "ExampleHardware",
    "ExampleList",
    "ExampleModel",
    "ExampleObjective",
    "ExampleSummary",
    "ExampleTask",
    "ExampleWorkload",
    "ExamplesDestinationReply",
    "ExamplesDestinationRequest",
    "ProjectTaskGoalReply",
    "ProjectTaskGoalRequest",
    "TaskGoal",
    "TaskList",
    "TaskSummary",
    "TasksListReply",
    "TasksListRequest",
    "bounded_goal",
    "read_task_goal",
]
