"""Shared errors for the VibeSys project boundary."""

from pathlib import Path


class ProjectError(RuntimeError):
    """Base class for invalid project layout or state operations."""


class ProjectStateError(ProjectError):
    """Raised when project metadata is missing, unsafe, or invalid."""


class StateModelNotFoundError(ProjectStateError):
    """Raised when a required model is absent from a state namespace."""


class RunSchemaMigrationRequiredError(ProjectStateError):
    """Raised when a run manifest predates the current run schema version.

    Callers own the operator-facing migration instructions; this package
    reports only the offending path and the two schema versions.
    """

    def __init__(self, message: str, *, path: Path, run_id: str, recorded_version: int) -> None:
        """Initialize the migration error and its structured context."""
        super().__init__(message)
        self.path = path
        self.run_id = run_id
        self.recorded_version = recorded_version
