"""What `WebInstanceHold.holders` can report on the host running the tests.

`holders` lists the processes with a file open under an instance directory by reading per-process
descriptors (procfs). A host without them (macOS) answers `None` by contract and `log_locked` is
then the only evidence, so a test that names a holder states what its own host can observe.
"""

from __future__ import annotations

from pathlib import Path

PER_PROCESS_DESCRIPTORS = Path("/proc/self/fd").is_dir()
"""Whether this host exposes the descriptors of other processes."""


def seen(*pids: int) -> tuple[int, ...] | None:
    """The `holders` this host reports when exactly ``pids`` have a file open under the directory."""
    return pids if PER_PROCESS_DESCRIPTORS else None
