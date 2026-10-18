"""Light contracts for the example catalog and the projects made from it or copied out.

``ExampleList`` is what ``vibesys example list --json`` prints and the helper's
``examples.list`` answers; ``CreatedProject`` is what ``project.create_from_example`` and
``project.copy_out`` answer. Pydantic and the standard library only, like
``server.host_api``, which embeds them in the helper's messages.

Public surface: ``ExampleSummary``, ``ExampleTask`` and what it holds, ``ExampleList``,
``CreatedProject``.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from server.host_preflight import Backend
from vs_project.api import AcceleratorVendor, ExampleDomain, ModelMaker

GOAL_MAX_CHARS: Final = 32_000
"""The most goal text an ``ExampleTask`` carries; a longer ``OBJECTIVE.md`` is cut and flagged."""


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


class ExampleTask(BaseModel):
    """What one task of an example is about, read from its ``vibesys.input.toml`` and goal file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task: str
    domain: ExampleDomain
    hardware: ExampleHardware
    model: ExampleModel | None
    """Present for ``llm-serving`` tasks."""
    workload: ExampleWorkload | None
    """Present for ``data-structures`` tasks."""
    objectives: tuple[ExampleObjective, ...]
    """Empty when the task has no ``objectives.toml``."""
    goal: Annotated[str, Field(max_length=GOAL_MAX_CHARS)]
    """The task's ``OBJECTIVE.md``, cut to ``GOAL_MAX_CHARS``."""
    goal_truncated: bool


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


__all__ = [
    "GOAL_MAX_CHARS",
    "CreatedProject",
    "ExampleHardware",
    "ExampleList",
    "ExampleModel",
    "ExampleObjective",
    "ExampleSummary",
    "ExampleTask",
    "ExampleWorkload",
]
