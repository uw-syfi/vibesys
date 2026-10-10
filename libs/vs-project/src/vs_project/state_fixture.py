"""A state directory below a project's ``.vibesys`` layout, for fixtures that need no run."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_project import _paths as project_paths

if TYPE_CHECKING:
    from pathlib import Path


def scratch_state_directory(project_root: Path, name: str) -> Path:
    """Create and return the directory ``name`` below the project's state layout.

    For a test double that persists through a ``StateNamespace`` without a run record.
    The layout stays owned by this library; callers never spell the path.
    """
    directory = project_root / project_paths.STATE_DIRECTORY_PATH / name
    directory.mkdir(parents=True, exist_ok=True)
    return directory
