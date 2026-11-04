"""Readers of the small result files a Slurm job's script leaves beside its stage results."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

SERVICE_LOG_TAIL = "service-log-tail.txt"
NODE_NAME = "node.txt"
_MAX_NODE_NAME = 255
_SERVICE_LOG_REPORTED_LINES = 40
_SERVICE_LOG_LINE_CHARS = 400
_SERVICE_LOG_TAIL_CHARS = 8_000


def read_node(path: Path) -> str | None:
    """The node name the job wrote, or None when it wrote none (it is best effort)."""
    try:
        name = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return name if name.isprintable() and name and len(name) <= _MAX_NODE_NAME else None


def distinct_tail(path: Path) -> str:
    """Return the service log's last lines, keeping only the latest copy of a repeated line.

    Servers often repeat one warning hundreds of times; without this the tail
    would show only that warning and hide the server's own progress lines.
    """
    if not path.is_file():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    seen: set[str] = set()
    kept: list[str] = []
    size = 0
    for raw_line in reversed(lines):
        line = (
            raw_line
            if len(raw_line) <= _SERVICE_LOG_LINE_CHARS
            else raw_line[:_SERVICE_LOG_LINE_CHARS] + " [line truncated]"
        )
        if line in seen:
            continue
        size += len(line) + 1
        if size > _SERVICE_LOG_TAIL_CHARS:
            break
        seen.add(line)
        kept.append(line)
        if len(kept) == _SERVICE_LOG_REPORTED_LINES:
            break
    return "\n".join(reversed(kept))
