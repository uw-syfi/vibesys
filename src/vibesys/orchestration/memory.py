"""Canonical workspace paths declared as orchestration-owned memory."""

from pathlib import Path

_MEMORY_ROOTS = ("roadmap", "progress")


def framework_memory_paths(workspace: Path) -> tuple[Path, ...]:
    """Resolve every orchestration-owned memory root in one workspace."""
    return tuple(workspace / name for name in _MEMORY_ROOTS)


def declared_memory_paths() -> tuple[str, ...]:
    """Return the workspace-relative memory roots declared by a plugin."""
    return _MEMORY_ROOTS
