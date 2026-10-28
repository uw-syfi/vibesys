"""What changed between two workspace revisions: the user's files, and the diff a record keeps.

Git's name-status output is parsed and filtered here once, for the run record's readers
(``vibesys.api``) and for the run itself, which keeps each settled attempt's changes on its
workstream span. The run makes each attempt's revision commits in its project's repository on the
machine the run process runs on (a Slurm run sends only its gates elsewhere), so the run reads the
range there, through the Git tracker it commits with.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict

from vs_project.api import is_project_state_path

if TYPE_CHECKING:
    from collections.abc import Sequence

DIFF_LIMIT = 4 << 20
"""Bytes of an attempt's diff its record keeps: enough for any change a person reads, small
enough that one attempt cannot grow the run's content without bound. The file list is kept whole."""


class WorkspaceChangeKind(StrEnum):
    """A semantic file change between two recorded workspace revisions."""

    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"


class WorkspaceChange(BaseModel):
    """One user-visible workspace change between immutable checkpoints."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    kind: WorkspaceChangeKind
    renamed_from: str | None = None


@dataclass(frozen=True)
class AttemptChanges:
    """What one attempt changed: every file, and the unified diff, cut at ``DIFF_LIMIT``."""

    files: tuple[WorkspaceChange, ...]
    diff: str


def parse_name_status(output: str) -> tuple[WorkspaceChange, ...]:
    """Parse Git's NUL-delimited name-status format into semantic changes."""
    tokens = output.split("\0")
    changes: list[WorkspaceChange] = []
    index = 0
    while index < len(tokens):
        status = tokens[index]
        if not status:
            break
        if status.startswith(("R", "C")) and index + 3 <= len(tokens):
            renamed_from, path = tokens[index + 1], tokens[index + 2]
            index += 3
            kind = (
                WorkspaceChangeKind.RENAMED if status.startswith("R") else WorkspaceChangeKind.ADDED
            )
            if kind is not WorkspaceChangeKind.RENAMED:
                renamed_from = ""
        elif index + 1 < len(tokens):
            path = tokens[index + 1]
            renamed_from = ""
            index += 2
            kind = {
                "A": WorkspaceChangeKind.ADDED,
                "D": WorkspaceChangeKind.DELETED,
            }.get(status[:1], WorkspaceChangeKind.MODIFIED)
        else:
            break
        changes.append(WorkspaceChange(path=path, kind=kind, renamed_from=renamed_from or None))
    return tuple(changes)


def capped_diff(diff: str) -> str:
    """``diff`` within ``DIFF_LIMIT`` bytes, cut on a line and closed by a line saying so."""
    whole = diff.encode()
    if len(whole) <= DIFF_LIMIT:
        return diff
    kept = whole[:DIFF_LIMIT]
    kept = kept[: kept.rfind(b"\n") + 1]
    marker = f"[diff truncated: {len(kept)} of {len(whole)} bytes shown]\n"
    return kept.decode(errors="ignore") + marker


class RevisionDiffs(Protocol):
    """Read-only diffs between two commits; ``vs_project.api.GitTracker`` is one."""

    def diff_name_status(self, base: str, head: str) -> str | None:
        """The NUL-delimited name-status of the range; None when it cannot be read."""
        ...

    def diff_patch(self, base: str, head: str, paths: Sequence[str]) -> str | None:
        """The unified diff of ``paths`` in the range; None when it cannot be read."""
        ...


def is_user_change(change: WorkspaceChange, framework: Sequence[str]) -> bool:
    """Whether ``change`` touches the user's files: not project state, not a ``framework`` directory."""
    paths = (change.path,) if change.renamed_from is None else (change.path, change.renamed_from)
    return not any(
        is_project_state_path(path)
        or any(path == prefix or path.startswith(f"{prefix}/") for prefix in framework)
        for path in paths
    )


def read_attempt_changes(
    git: RevisionDiffs, base: str, head: str, *, framework: Sequence[str] = ()
) -> AttemptChanges | None:
    """The user's files ``base..head`` changed and their diff; None when the range is unreadable.

    A value that is not a plain commit name (an older run's placeholder) reads as unreadable.
    """
    try:
        listed = git.diff_name_status(base, head)
        if listed is None:
            return None
        files = tuple(
            change for change in parse_name_status(listed) if is_user_change(change, framework)
        )
        if not files:
            return AttemptChanges(files=(), diff="")
        paths = [path for change in files for path in (change.renamed_from, change.path) if path]
        diff = git.diff_patch(base, head, paths)
    except ValueError:
        return None
    return AttemptChanges(files=files, diff="" if diff is None else capped_diff(diff))
