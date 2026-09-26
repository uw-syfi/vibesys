"""Application boundary joining typed project state with Git history."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from framework.api import Project, StateNamespace
    from vibesys.run.git_tracker import GitTracker


@dataclass(frozen=True)
class RunState:
    """Typed access to one run's portable and machine-local state.

    ``Project.state`` owns path resolution and filesystem serialization.
    ``GitTracker`` receives only immutable snapshots produced by that store.
    """

    project: Project
    git: GitTracker
    run_id: str

    def __post_init__(self) -> None:
        """Require state persistence and Git history to identify the same run."""
        if self.project.root != self.git.history_root:
            message = f"run state project root does not match Git history root: {self.project.root} != {self.git.history_root}"
            raise ValueError(message)
        if self.run_id != self.git.run_id:
            _exception_message = f"run state ID does not match Git history run ID: {self.run_id!r} != {self.git.run_id!r}"
            raise ValueError(_exception_message)

    def portable(self, namespace: str) -> StateNamespace:
        """Return the portable state handle for ``namespace``."""
        return self.project.state.portable_namespace(self.run_id, namespace)

    def local(self, namespace: str) -> StateNamespace:
        """Return the machine-local state handle for ``namespace``."""
        return self.project.state.local_namespace(self.run_id, namespace)

    def commit(self, label: str, namespace: StateNamespace) -> None:
        """Commit the exact current contents of one portable namespace."""
        self.git.snapshot_framework_state(label, namespace.snapshot())
