"""Remember each check's verdict per exact candidate tree, so an unchanged tree is not rerun.

The check is deterministic for a given tree and options, so running it again cannot tell the
agent anything new: it only costs 10 to 120 s and the agent's tokens (live-1: 14 runs in one
implementer turn, most re-running to see a different slice of the output; a judge re-ran
it 12 times). A verdict is the exit code and the full report, stored under a key that is the
hash of every file in the candidate root and the check's options, except the root's dot
entries (the framework puts its per-session `.git` link, `.mcp.json` token and agent
settings there, which differ between an implementer's and a judge's checkout of one tree). Any edit to the candidate or
to the check changes the key, so a stored verdict never describes a different tree. Only exits
0 (pass) and 1 (a round failed) are stored; exit 2 (server did not start) may be an
environment fault, so it is rerun.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

STORED_EXITS = frozenset({0, 1})
_SKIPPED_DIRS = frozenset({".git", "__pycache__", ".venv", ".pytest_cache", ".mypy_cache"})


def tree_key(root: Path, options: dict[str, object]) -> str:
    """Hash of the candidate root's files (paths and bytes) and the check's options."""
    digest = hashlib.sha256(json.dumps(options, sort_keys=True).encode())
    for directory, subdirs, files in os.walk(root):
        at_root = Path(directory) == root
        subdirs[:] = sorted(
            d for d in subdirs if d not in _SKIPPED_DIRS and not (at_root and d.startswith("."))
        )
        for name in sorted(files):
            if at_root and name.startswith("."):
                continue
            path = Path(directory, name)
            digest.update(b"\0" + path.relative_to(root).as_posix().encode() + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


@dataclass(frozen=True)
class Verdict:
    exit_code: int
    report: str


class Verdicts:
    """A directory of stored verdicts, one file per tree key."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def report_path(self, key: str) -> Path:
        """The stored report as plain text, for an agent to read or grep without rerunning."""
        return self.directory / f"{key}.txt"

    def lookup(self, key: str) -> Verdict | None:
        try:
            data = json.loads(self.path(key).read_text())
            return Verdict(exit_code=int(data["exit_code"]), report=str(data["report"]))
        except (OSError, ValueError, KeyError, TypeError):
            return None  # absent or unreadable: run the check

    def record(self, key: str, verdict: Verdict) -> None:
        if verdict.exit_code not in STORED_EXITS:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.path(key)
        pending = target.with_suffix(f".{os.getpid()}.tmp")
        pending.write_text(json.dumps({"exit_code": verdict.exit_code, "report": verdict.report}))
        pending.replace(target)  # readers never see a partial verdict
        self.report_path(key).write_text(verdict.report + "\n")
