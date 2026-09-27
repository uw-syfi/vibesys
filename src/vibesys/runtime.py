"""Private workspace-scope identity used by the VibeSys composition host."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class WorkspaceScope:
    """An isolated candidate tree and its current retained revision."""

    id: str
    path: Path
    revision: str


__all__ = ["WorkspaceScope"]
