#!/usr/bin/env python3
"""Stop gate for optimization runs. Unreadable state fails open.

A current idle reason or full slots release search turns. Running tasks do not
excuse unexplained free slots. Other sessions release after session ownership binds.
Crons and stop_hook_active never bypass the gate. The run deadline bounds blocking;
Claude Code additionally caps consecutive tool-free continuations at eight.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "search/optimize/scripts"))
from run import parse_date, read_state, status

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["block_reason", "main"]


def block_reason(cwd: Path, now: datetime, event: dict[str, Any] | None = None) -> str | None:
    """Return a state-derived continuation instruction, or None to release."""
    event = event or {}
    try:
        root = next((p for p in (cwd, *cwd.parents) if (p / ".vibesys-optimize").is_dir()), cwd)
        home = root / ".vibesys-optimize"
        run_id = (home / "active").read_text().strip()
        if not run_id or any(c in run_id for c in ("/", "\\")) or run_id in {".", ".."}:
            return None
        run = home / run_id
        with (run / "run.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = read_state(run)
            session = event.get("session_id")
            if not session or state["session_id"] not in {None, session}:
                return None
            if state["session_id"] is None:
                state["session_id"] = session
                temporary = run / "session.tmp"
                temporary.write_text(json.dumps(state, indent=2) + "\n")
                temporary.replace(run / "run.json")
        summary = status(state, now)
        if summary["seconds_left"] <= 0 or state["finished"]:
            return None
        if now < parse_date(state["wrap_up_at"]) and (
            state["idle_reason"] or not summary["free_slots"]
        ):
            return None
        return summary["next"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def main(
    stdin: TextIO,
    out: TextIO,
    *,
    now: Callable[[], datetime],
    fallback_cwd: Path,
    project_dir: Path | None = None,
) -> int:
    """Handle one Stop input. All time and streams are supplied by the caller."""
    try:
        event = json.loads(stdin.read() or "{}")
        cwd = Path(event["cwd"]) if isinstance(event.get("cwd"), str) else fallback_cwd
        reason = block_reason(project_dir or cwd, now(), event)
        if reason:
            out.write(json.dumps({"decision": "block", "reason": reason}) + "\n")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(
        main(
            sys.stdin,
            sys.stdout,
            # LW-100358 [TID251]; standalone host clock; vs_sim adds a dependency, indirect lookup hides real I/O.
            now=lambda: datetime.now(timezone.utc),  # noqa: TID251
            fallback_cwd=Path.cwd(),
            project_dir=Path(os.environ["CLAUDE_PROJECT_DIR"])
            if os.environ.get("CLAUDE_PROJECT_DIR")
            else None,
        )
    )
