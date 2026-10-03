"""Canonical workspace paths declared as orchestration-owned memory."""

_MEMORY_ROOTS = ("roadmap", "progress")


def declared_memory_paths() -> tuple[str, ...]:
    """Return the workspace-relative memory roots declared by a plugin."""
    return _MEMORY_ROOTS
