"""Every run recorded under one project, read the same whichever branch is checked out.

A run keeps its manifest and the other state it commits on its own run branch, and the
state it never commits in the project's checkout. Only the run whose branch is checked
out sees all of that in the checkout. ``RunHistory`` is how anything else (a run list,
a record of an ended run, the choice of a run to resume) reads a run, without changing
the checkout.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import PurePosixPath

    from vs_project._manifests import OrchestrationRunManifest
    from vs_project._state import StateFile
    from vs_project.errors import ProjectStateError


@dataclass(frozen=True, slots=True)
class RecordedRuns:
    """The runs a history holds: the readable ones and why each other one is not."""

    runs: tuple[OrchestrationRunManifest, ...]
    """Every readable run's manifest, oldest first (by creation time, then run id)."""

    unreadable: tuple[tuple[str, ProjectStateError], ...]
    """``(run_id, error)`` for each recorded run whose manifest this VibeSys cannot read
    (an older schema, damage), ordered by run id."""


class RunHistory(Protocol):
    """Read-only access to every run recorded under one project.

    A run is recorded when it has a manifest. Reads never change the checkout, the
    index, or any ref, so a run list or a run record reads the same before and after
    the project's own branch is checked out again. Run ids are validated first: an
    invalid one raises ``ProjectStateError``.
    """

    def recorded(self) -> RecordedRuns:
        """Every recorded run."""
        ...

    def manifest(self, run_id: str) -> OrchestrationRunManifest:
        """One run's manifest; ``ProjectStateError`` when it is not recorded or not readable."""
        ...

    def document(self, run_id: str, relative_path: str | PurePosixPath) -> bytes | None:
        """One portable state file of a run, by path within the run's directory, or ``None``."""
        ...

    def documents(self, run_id: str) -> tuple[StateFile, ...]:
        """Every portable state file of a run, relative to the run's directory, by path."""
        ...
