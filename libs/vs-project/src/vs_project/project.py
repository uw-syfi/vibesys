"""Public aggregate for one repository-native VibeSys project."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Self

from vs_project._git_process import git_environment, run_git
from vs_project._layout import (
    ConfigurationRoot,
    ProjectLayout,
    ProjectLayoutError,
    TaskDirectory,
    TaskName,
    TasksRoot,
)
from vs_project._state import ProjectState
from vs_project._state_store import LocalStateStore

if TYPE_CHECKING:
    from collections.abc import Iterable

    from vs_project.api.state_store import CommitFault


class Project:
    """One canonical project root with authored tasks and generated state."""

    def __init__(self, layout: ProjectLayout) -> None:
        """Bind layout and state operations to the same validated root."""
        self._layout = layout
        self._state: ProjectState | None = None

    @classmethod
    def open(cls, project_root: Path | str) -> Self:
        """Open an existing directory as a project.

        Authored task configuration and generated state may both be absent.
        Operations that need either surface validate it when called.
        """
        return cls(ProjectLayout.open(project_root))

    @classmethod
    def discover(cls, start: Path | str) -> Self:
        """Find the closest task-configured project at or above an existing path."""
        return cls(ProjectLayout.discover(start))

    @property
    def root(self) -> Path:
        """Return the absolute, resolved candidate repository root."""
        return self._layout.project_root.path

    @property
    def state(self) -> ProjectState:
        """Return state operations bound to this project's canonical root."""
        if self._state is None:
            self._state = ProjectState(self.root)
        return self._state

    def state_store(
        self, run_id: str, *, fault_plan: Iterable[CommitFault] = ()
    ) -> LocalStateStore:
        """Open the shared atomic record and host fence for one validated run."""
        return LocalStateStore(self, run_id, fault_plan=fault_plan)

    def is_initialized(self) -> bool:
        """Return whether this project has repository-native task configuration."""
        return self._layout.is_initialized()

    def configuration_root(self) -> ConfigurationRoot:
        """Return the validated human-authored configuration root."""
        return self._layout.configuration_root()

    def configuration_path(self) -> Path:
        """Return the canonical configuration path, whether or not it exists."""
        return self._layout.configuration_path()

    def tasks_root(self) -> TasksRoot:
        """Return the validated root containing task definitions."""
        return self._layout.tasks_root()

    def discover_tasks(self) -> tuple[TaskDirectory, ...]:
        """Return all validated task definitions ordered by name."""
        return self._layout.discover_tasks()

    def select_task(self, task_name: TaskName | str | None = None) -> TaskDirectory:
        """Select a task explicitly, or implicitly when exactly one exists."""
        return self._layout.select_task(task_name)

    @classmethod
    def is_state_initialized(cls, path: Path | str) -> bool:
        """Return whether a directory contains initialized generated state."""
        return ProjectState.is_project_root(path)

    @classmethod
    def find_state_projects(cls, collection: Path | str) -> tuple[Path, ...]:
        """Return state-initialized projects directly below a collection."""
        return ProjectState.find_projects(collection)

    @staticmethod
    def validate_collection_root(collection: Path) -> None:
        """Reject a collection whose child projects would nest inside a Git repository."""
        root = collection.expanduser().resolve()
        for ancestor in (root, *root.parents):
            if not ancestor.is_dir() or not (ancestor / ".git").exists():
                continue
            try:
                result = run_git(
                    ["rev-parse", "--show-toplevel"],
                    cwd=ancestor,
                    env=git_environment(safe_directory=ancestor),
                    text=True,
                    timeout=10.0,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                message = f"could not validate project collection {root}: {exc}"
                raise ProjectLayoutError(message) from exc
            if result.returncode == 0:
                repository = Path(result.stdout.strip()).resolve()
                message = (
                    f"project collection {root} is inside Git repository {repository}; "
                    "each copied project must own its repository"
                )
                raise ProjectLayoutError(message)

    @classmethod
    def log_directory_for(cls, project_root: Path | str, run_id: str) -> Path:
        """Return a run log destination, including before a root is materialized."""
        return ProjectState.log_directory_for(project_root, run_id)

    @classmethod
    def agent_homes_directory_for(cls, project_root: Path | str, run_id: str) -> Path:
        """Return the machine-local root of one run's dedicated agent CLI homes."""
        return ProjectState.agent_homes_directory_for(project_root, run_id)
