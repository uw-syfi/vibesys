"""Where a project keeps comparison baseline readings and the cluster state of measuring them."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from vs_project._paths import STATE_DIRECTORY_POSIX
from vs_project._state import StateNamespace

if TYPE_CHECKING:
    from pathlib import Path

    from vs_project._state import ProjectState

_NAME = "comparisons"


def comparison_directory(state: ProjectState) -> Path:
    """The machine-local directory holding this project's comparison readings.

    It lives with the project's other machine-local caches: never committed, and below the
    ``local`` state the agent cannot see.
    """
    return state.model_cache_directory(_NAME)


def comparison_namespace(state: ProjectState) -> StateNamespace:
    """The machine-local namespace for the cluster jobs' records of measuring comparisons."""
    directory = comparison_directory(state)
    (directory / "evaluations").mkdir(parents=True, exist_ok=True)
    return StateNamespace(
        project_root=state.project_root,
        root=directory / "evaluations",
        portable=False,
        containment_root=directory,
        namespace_root=STATE_DIRECTORY_POSIX / "local" / _NAME / PurePosixPath("evaluations"),
    )
