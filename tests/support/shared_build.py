"""Build an expensive test artifact once per pytest session, not once per xdist worker.

A session-scoped fixture runs once in each xdist worker, so a native build in
one repeats up to ``-n`` times. ``shared_build`` gives every worker the same
directory and lets the first one in build it under an exclusive lock; the rest
find the finished build and reuse it. Without xdist it is an ordinary per-session
build directory. The directory is under the session's base temp directory, so a
later session never sees an earlier build.
"""

from __future__ import annotations

import fcntl
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

_DONE = ".shared-build-done"


def _session_directory(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.getbasetemp()
    # xdist workers use ``<session>/popen-gwN``; the controller's ``<session>`` is shared.
    return base.parent if os.environ.get("PYTEST_XDIST_WORKER") else base


def shared_build(
    tmp_path_factory: pytest.TempPathFactory,
    name: str,
    build: Callable[[Path], None],
) -> Path:
    """Return the directory ``build`` filled, running ``build`` once per session.

    ``build`` receives a fresh empty directory and must leave a complete result
    in it; if it raises, nothing is marked done and the next caller builds again.
    Callers treat the result as read-only because other workers share it.
    """
    session = _session_directory(tmp_path_factory)
    directory = session / f"shared-build-{name}"
    with (session / f"shared-build-{name}.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if not (directory / _DONE).exists():
                directory.mkdir(parents=True, exist_ok=True)
                build(directory)
                (directory / _DONE).touch()
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return directory
