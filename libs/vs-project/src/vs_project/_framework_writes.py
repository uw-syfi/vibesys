"""A record of what the framework itself wrote, so agent writes can be told apart.

Framework state lives inside the agent-visible worktree (below ``.vibesys``) and the
framework rewrites it while an agent turn is in flight. A role that may not write
must still be caught writing there, so the isolation check cannot ignore the
directory. It compares each changed path with what the framework last left in it:
a file whose bytes equal the last framework write is the framework's, any other
change is attributed to the agent.

The record is process-wide because independent ``ProjectState`` and ``GitTracker``
objects over one project must agree on it. It is keyed by absolute path.

The check runs on another thread than the write (the isolation check of a read-only
turn races the framework's own commits), so the record must never be behind the
file. ``publishing`` therefore records the incoming bytes *before* the file changes and
keeps the outgoing bytes valid until the change is done; ``is_framework_state`` reads
the record and the file under one lock. Recording after the replace, or comparing a
record read earlier with a file read later, would let a check land between the two and
blame the agent for the framework's own write.
"""

from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
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
        # Every digest the file may hold right now and still be the framework's own:
        # one outside a publication, two (outgoing and incoming) during it. ``None``
        # stands for "the file is absent".
        self._files: dict[Path, frozenset[str | None]] = {}
        self._directories: set[Path] = set()
        # Paths as claimed, so claiming one again costs no ``realpath``. Claims are
        # idempotent and a directory is only ever claimed after its caller has proven
        # it is a plain directory below its owning root.
        self._claimed: set[Path] = set()

    @contextmanager
    def publishing(self, path: Path, contents: bytes | None) -> Iterator[None]:
        """Cover the framework replacing *path* with *contents*, or deleting it for ``None``.

        The body performs the replacement. While it runs the file may hold either the
        outgoing or the incoming bytes and both are the framework's. A body that
        raises may or may not have replaced the file, so both stay valid then.
        """
        key = _key(path)
        incoming = None if contents is None else _digest(contents)
        with self._lock:
            outgoing = self._files.get(key, frozenset())
            self._files[key] = outgoing | {incoming}
        yield
        with self._lock:
            self._files[key] = frozenset({incoming})

    def claim_directory(self, path: Path) -> None:
        """Record a directory handed to a path-based library that writes in it itself.

        Files in it that the framework published through ``publishing`` are still compared
        by digest; any other file in it is the library's.
        """
        with self._lock:
            if path in self._claimed:
                return
        resolved = path.resolve()
        with self._lock:
            self._directories.add(resolved)
            self._claimed.add(path)

    def is_framework_state(self, path: Path) -> bool:
        """Return whether *path* is exactly as the framework left it.

        The record and the file are read under the lock a publication takes to start
        and to finish, so a check never pairs a stale record with a newer file.
        """
        key = _key(path)
        with self._lock:
            expected = self._files.get(key)
            if expected is not None:
                return any(_matches(key, digest) for digest in expected)
            return any(key.is_relative_to(directory) for directory in self._directories)


def _matches(path: Path, expected: str | None) -> bool:
    if expected is None:
        return not path.exists() and not path.is_symlink()
    try:
        return path.is_file() and not path.is_symlink() and _digest(path.read_bytes()) == expected
    except OSError:
        return False


FRAMEWORK_WRITES = FrameworkWrites()
