"""Read back the plain-text run logs that ``RunLogger`` writes.

The run log directory holds ``run-<timestamp>[-<label>].log`` files. This
module is their one reader, so consumers such as the server's inspector ask
for "the newest run log" and never list or open the directory themselves.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

RUN_LOG_GLOB = "run-*.log"
"""Names ``RunLogger`` gives its files: ``run-<timestamp>[-<label>].log``."""

RUN_LOG_TAIL_BYTES = 8 * 1024 * 1024
"""How much of the end of a run log a reader receives (8 MiB).

A long run's log grows without bound; readers look for recent lines, so they
get the tail rather than a whole file loaded into memory.
"""


@dataclass(frozen=True)
class RunLogText:
    """The decoded tail of one run log."""

    name: str
    text: str
    truncated: bool


def read_latest_run_log(log_dir: Path, *, max_bytes: int = RUN_LOG_TAIL_BYTES) -> RunLogText | None:
    """Return the tail of the newest regular run log in ``log_dir``, if any.

    Symbolic links and non-files are skipped. At most ``max_bytes`` from the
    end of the file are read; invalid UTF-8 is replaced, not raised. A file
    that disappears or cannot be read yields ``None``, as does a missing
    directory.
    """
    try:
        candidates = sorted(
            path for path in log_dir.glob(RUN_LOG_GLOB) if path.is_file() and not path.is_symlink()
        )
    except OSError:
        return None
    if not candidates:
        return None
    latest = candidates[-1]
    try:
        with latest.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            start = max(0, size - max_bytes)
            handle.seek(start)
            data = handle.read(max_bytes)
    except OSError:
        return None
    return RunLogText(
        name=latest.name,
        text=data.decode("utf-8", errors="replace"),
        truncated=start > 0,
    )
