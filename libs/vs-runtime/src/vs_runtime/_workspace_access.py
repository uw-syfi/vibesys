"""Shared workspace-access policy for runtime sessions and their fakes."""

from __future__ import annotations


def unauthorized_paths(
    changes: list[str],
    allowed: tuple[str, ...],
    *,
    directories: tuple[str, ...] = (),
) -> list[str]:
    """Return changed paths outside the exact-path and directory grants."""
    return [
        path
        for path in changes
        if not any(path == item for item in allowed)
        and not any(path == item or path.startswith(f"{item}/") for item in directories)
    ]


__all__ = ["unauthorized_paths"]
