"""A record of what the framework itself wrote, so agent writes can be told apart.

Framework state lives inside the agent-visible worktree (below ``.vibesys``) and the
framework rewrites it while an agent turn is in flight. A role that may not write
must still be caught writing there, so the isolation check cannot ignore the
directory. It compares each changed path with what the framework last left in it:
a file whose bytes equal the last framework write is the framework's, any other
change is attributed to the agent.

The record is process-wide because independent ``ProjectState`` and ``GitTracker``
objects over one project must agree on it. It is keyed by absolute path.
"""

from __future__ import annotations

import hashlib
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def _key(path: Path) -> Path:
    """Normalize *path* without requiring that it exists."""
    return path.parent.resolve() / path.name


def _digest(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


class FrameworkWrites:
    """Digests of framework-written files and framework-owned external directories."""

    def __init__(self) -> None:
        """Start with no recorded writes."""
        self._lock = threading.Lock()
        self._files: dict[Path, str | None] = {}
        self._directories: set[Path] = set()

    def wrote(self, path: Path, contents: bytes) -> None:
        """Record that the framework published *contents* at *path*."""
        key = _key(path)
        with self._lock:
            self._files[key] = _digest(contents)

    def removed(self, path: Path) -> None:
        """Record that the framework deleted the file at *path*."""
        key = _key(path)
        with self._lock:
            self._files[key] = None

    def claim_directory(self, path: Path) -> None:
        """Record a directory handed to a path-based library that writes in it itself.

        Files in it that the framework published through ``wrote`` are still compared
        by digest; any other file in it is the library's.
        """
        resolved = path.resolve()
        with self._lock:
            self._directories.add(resolved)

    def is_framework_state(self, path: Path) -> bool:
        """Return whether *path* is exactly as the framework left it."""
        key = _key(path)
        with self._lock:
            known = key in self._files
            expected = self._files.get(key)
            claimed = any(key.is_relative_to(directory) for directory in self._directories)
        if known:
            return _matches(key, expected)
        return claimed


def _matches(path: Path, expected: str | None) -> bool:
    if expected is None:
        return not path.exists() and not path.is_symlink()
    try:
        return path.is_file() and not path.is_symlink() and _digest(path.read_bytes()) == expected
    except OSError:
        return False


FRAMEWORK_WRITES = FrameworkWrites()
