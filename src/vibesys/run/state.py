"""Application boundary joining typed project state with Git history."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vibesys.run.git_tracker import GitTracker
    from vs_loop_state.api import RoundRecord
    from vs_project.api import Project, StateNamespace


class RunStateNamespace(StrEnum):
    """Framework-owned state namespaces for one run."""

    AGENT = "agent"
    EVOLVE = "evolve"
    PLAIN = "plain"
    RUNTIME = "runtime"
    SKYPILOT = "skypilot"


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
            raise ValueError(
                "run state project root does not match Git history root: "
                f"{self.project.root} != {self.git.history_root}"
            )
        if self.run_id != self.git.run_id:
            raise ValueError(
                f"run state ID does not match Git history run ID: {self.run_id!r} != "
                f"{self.git.run_id!r}"
            )

    def portable(self, namespace: RunStateNamespace) -> StateNamespace:
        """Return the portable state handle for ``namespace``."""
        return self.project.state.portable_namespace(self.run_id, namespace.value)

    def local(self, namespace: RunStateNamespace) -> StateNamespace:
        """Return the machine-local state handle for ``namespace``."""
        return self.project.state.local_namespace(self.run_id, namespace.value)

    def commit(self, label: str, namespace: StateNamespace) -> None:
        """Commit the exact current contents of one portable namespace."""
        self.git.snapshot_framework_state(label, namespace.snapshot())

    def completed_rounds(self) -> list[RoundRecord]:
        """Load the validated completed-round history for this run."""
        return self.project.state.load_rounds(self.run_id)
