"""Shared errors for the VibeSys project boundary."""


class ProjectError(RuntimeError):
    """Base class for invalid project layout or state operations."""


class ProjectStateError(ProjectError):
    """Raised when project metadata is missing, unsafe, or invalid."""


class StateModelNotFoundError(ProjectStateError):
    """Raised when a required model is absent from a state namespace."""
