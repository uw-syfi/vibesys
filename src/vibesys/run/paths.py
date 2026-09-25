"""Frozen value objects for per-run paths."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunPaths:
    """Canonical host paths owned by one project run.

    ``run_log_path`` is the *current* log file.  ``switch_log_file``
    replaces the whole record (the dataclass is frozen) rather than
    mutating the field in place.
    """

    project_root: Path
    log_dir: Path
    run_log_path: Path

    @property
    def workspace(self) -> Path:
        """Return the project root, which is also the only agent workspace."""
        return self.project_root
