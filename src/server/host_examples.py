"""Light contracts for the example catalog and the projects made from it or copied out.

``ExampleList`` is what ``vibesys example list --json`` prints and the helper's
``examples.list`` answers; ``CreatedProject`` is what ``project.create_from_example`` and
``project.copy_out`` answer. Pydantic and the standard library only, like
``server.host_api``, which embeds them in the helper's messages.

Public surface: ``ExampleSummary``, ``ExampleList``, ``CreatedProject``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


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


__all__ = ["CreatedProject", "ExampleList", "ExampleSummary"]
