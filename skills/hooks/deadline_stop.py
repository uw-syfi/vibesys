#!/usr/bin/env python3
"""Stop gate for optimization runs. Unreadable state fails open.

Only running subagents or explicitly tracked shell commands release search turns.
Crons and stop_hook_active never bypass the gate. The run deadline bounds blocking;
Claude Code additionally caps consecutive tool-free continuations at eight.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "search/measure/scripts"))
from run import read_state, status

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
        state = read_state(run)
        summary = status(state, now)
        if summary["seconds_left"] <= 0 or state["finished"]:
            return None
        wrap_up = datetime.fromisoformat(state["wrap_up_at"])
        pending = event.get("background_tasks", [])
        running = any(
            t.get("status") == "running"
            and (
                t.get("type") == "subagent"
                or (t.get("type") == "shell" and t.get("id") in state["commands"])
            )
            for t in pending
        )
        if now < wrap_up and running:
            return None
        return (
            f"{summary['seconds_left']:g} seconds left; wrap-up {summary['wrap_up_at']}; "
            f"{summary['free_slots']} free slots. {summary['next']}. "
            f'Run python3 "{Path(__file__).resolve().parents[1]}/search/measure/scripts/run.py" status --run "{run}"'
        )
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
            now=lambda: datetime.now(timezone.utc),
            fallback_cwd=Path.cwd(),
            project_dir=Path(os.environ["CLAUDE_PROJECT_DIR"])
            if os.environ.get("CLAUDE_PROJECT_DIR")
            else None,
        )
    )
