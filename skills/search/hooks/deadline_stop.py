#!/usr/bin/env python3
"""Claude Code Stop hook: keep a `vibesys:optimize` run going until its deadline.

Does nothing unless the session's working directory holds ``.vibesys-optimize/active``. That
file names the active run; the run's ``run.json`` holds the ``deadline`` (ISO 8601 with a
timezone). Before the deadline the hook blocks the stop and tells the lead to continue; at or
after it, or once the file is gone, the stop goes through.

It fails open: any problem reading the state allows the stop, so a broken run directory can
never trap a session. Standard library only (Python 3.10 or newer).

Reads the hook's JSON from stdin (the ``cwd`` field names the session's directory); a block is
``{"decision": "block", "reason": ...}`` on stdout, an allowed stop prints nothing. Exit code 0.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import TextIO

__all__ = ["block_reason", "main"]

STATE_DIR = ".vibesys-optimize"
ACTIVE_FILE = "active"
_FAIL_OPEN = (OSError, ValueError, KeyError, TypeError, AttributeError)
"""What reading the state can raise: unreadable or missing files, bad JSON or dates, wrong shapes."""


def block_reason(cwd: Path, now: datetime) -> str | None:
    """The reason to keep the session going, or None when the stop should go through."""
    active = cwd / STATE_DIR / ACTIVE_FILE
    try:
        run_id = active.read_text(encoding="utf-8").strip()
        if not run_id or "/" in run_id or "\\" in run_id or run_id in (".", ".."):
            return None
        run_dir = cwd / STATE_DIR / run_id
        stated = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["deadline"]
        # Python 3.10 does not read a trailing Z; 3.11 and later do.
        deadline = datetime.fromisoformat(
            stated.removesuffix("Z") + "+00:00" if stated[-1:] == "Z" else stated
        )
        remaining = (deadline - now).total_seconds()
    except _FAIL_OPEN:
        return None
    if remaining <= 0:
        return None
    minutes = math.ceil(remaining / 60)
    unit = "minute remains" if minutes == 1 else "minutes remain"
    return (
        f"{minutes} {unit} until the deadline: continue the vibesys:optimize loop from "
        f"{STATE_DIR}/{run_id}/notes; at wrap-up remove {STATE_DIR}/{ACTIVE_FILE}"
    )


def main(stdin: TextIO, out: TextIO, *, now: Callable[[], datetime], fallback_cwd: Path) -> int:
    """Answer one Stop event; always returns 0 (an error allows the stop)."""
    try:
        event = json.loads(stdin.read() or "{}")
        cwd = Path(event["cwd"]) if isinstance(event.get("cwd"), str) else fallback_cwd
        reason = block_reason(cwd, now())
    except _FAIL_OPEN:
        return 0
    if reason is not None:
        out.write(json.dumps({"decision": "block", "reason": reason}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(
        main(
            sys.stdin,
            sys.stdout,
            now=lambda: datetime.now(timezone.utc),
            fallback_cwd=Path.cwd(),
        )
    )
