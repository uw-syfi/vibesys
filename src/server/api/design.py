"""Project the per-round workspace changes behind a run's design log.

Only what the experiment log does not already carry: the files each round's
commit range touched. Every other per-round fact (outcome, review, official
evaluation, candidate disposition, measurement) crosses the protocol once, on
``HypothesisRound``, and clients join the two by round number.

File lists come from a read-only ``git diff`` over each round's net commit
range in the run's workspace, filtered down to the system under optimization:
the framework's own bookkeeping paths (run state, roadmap, progress notes) are
excluded, derived from the modules that own those layouts rather than restated
here. Per-file patch text is served on demand from the same ranges, gated by
the same filtered file lists.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from server.api.protocol import DesignFileChange, DesignPatch, DesignRound
from vibesys.api import framework_memory_paths
from vs_project.api import is_project_state_path

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.api import RunView

#: Round checkpoints and the trusted baseline are recorded as commit hashes.
#: Anything else (legacy placeholders, corrupt state) must not reach the git
#: command line, where a leading "-" would read as an option.
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{7,64}$")

#: Distinct commit ranges kept per run. A round's range is immutable once its
#: checkpoint exists, so entries never go stale; the bound exists only so a
#: very long run cannot grow the cache without limit.
_CACHE_CAPACITY = 512

#: Cached per-file patches. Entries can each reach the character limit, so
#: this bound is what caps the cache's memory, not the entry count itself.
_PATCH_CACHE_CAPACITY = 64

#: Upper bound on one patch's text. Big enough for any hand-reviewable
#: change, small enough that a response and its terminal rendering stay
#: cheap; the truncation is marked so a client can point at the exact
#: ``git diff`` command for the rest.
_PATCH_CHAR_LIMIT = 200_000

_CHANGE_BY_STATUS: dict[str, Literal["added", "modified", "deleted"]] = {
    "A": "added",
    "D": "deleted",
}

#: A ``git diff --name-status`` record for a rename or copy carries two paths.
_PAIRED_STATUS_FIELDS = 3

DiffNameStatus = Callable[[str, str], str | None]
"""Read-only ``git diff --name-status -z`` between two commits, or None."""

DiffPatch = Callable[[str, str, tuple[str, ...]], str | None]
"""Read-only ``git diff`` patch between two commits for given paths, or None."""


class DesignLog:
    """Per-round file changes for one attached run, with a bounded diff cache.

    The projection is pure apart from ``diff`` and ``patch``, the two
    read-only git callables it is wired with (the run's own
    :class:`~vibesys.run.git_tracker.GitTracker` name-status read and the
    server's :class:`~server.api.workspace_git.WorkspacePatchReader`).
    Results are cached by ``(base, head)`` and ``(base, head, path)``: all
    are immutable checkpoints, so a cached entry stays correct and nothing
    invalidates it. Only successes are cached, so a transient git failure
    does not become permanent.
    """

    def __init__(
        self,
        *,
        workspace: Path,
        diff: DiffNameStatus,
        patch: DiffPatch,
        capacity: int = _CACHE_CAPACITY,
    ) -> None:
        """Bind the projection to one workspace and its git read paths."""
        self._diff = diff
        self._diff_patch = patch
        self._capacity = capacity
        self._framework_paths = _framework_prefixes(workspace)
        self._cache: OrderedDict[tuple[str, str], list[DesignFileChange]] = OrderedDict()
        self._patch_cache: OrderedDict[tuple[str, str, str], DesignPatch] = OrderedDict()

    def rounds(self, state: RunView, *, baseline: str) -> list[DesignRound]:
        """Return one entry per recorded round, in round order.

        Each round's file list covers exactly the range that round produced: a
        hypothesis's first round starts from the hypothesis's parent checkpoint
        (which differs from the chronologically previous round when the
        orchestrator reverted), every later round from the same hypothesis's
        previous round. ``baseline`` is the run manifest's trusted input
        baseline, the commit the run branched from, and anchors hypotheses that
        recorded no parent of their own.

        ``state`` is the `vibesys.api.RunView` for the attached run: its
        `HypothesisView.rounds`/`RunView.rounds` carry the same
        `round_number`/`commit`/`parent_commit` facts this projection used to
        read off the core `AgentRunState` directly.
        """
        chronological = _chronological_bases(state, baseline)
        entries: list[DesignRound] = []
        for hypothesis in state.hypotheses:
            previous = _commit(hypothesis.parent_commit)
            for record in hypothesis.rounds:
                base = previous if previous is not None else chronological.get(record.round_number)
                commit = _commit(record.commit)
                files = (
                    self._changed_files(base, commit)
                    if base is not None and commit is not None
                    else None
                )
                entries.append(
                    DesignRound(
                        round=record.round_number,
                        commit=_text(record.commit),
                        base=base,
                        files=files,
                    )
                )
                if commit is not None:
                    previous = commit
        return sorted(entries, key=lambda entry: entry.round)

    def patch(self, base: str, head: str, path: str) -> DesignPatch:
        """Return one file's bounded patch from an already-published range.

        ``base`` and ``head`` must be plain commit object names, and ``path``
        must be one of the files :meth:`rounds` listed for that range;
        anything else raises ``ValueError``. Routing the membership check
        through the same filtered file list keeps the framework-path
        exclusion authoritative: a filtered path is indistinguishable from
        one the range never touched, and no unvetted path reaches git.
        """
        for value in (base, head):
            if _COMMIT_PATTERN.fullmatch(value) is None:
                raise ValueError(f"not a commit object name: {value!r}")  # noqa: TRY003  # Names the rejected value.
        changes = self._changed_files(base, head)
        if changes is None:
            # The range's file list itself is unreadable (repository gone or
            # never held these objects). There is nothing to validate the
            # path against and no patch to read; report the absence so the
            # client can explain it instead of failing the request.
            return DesignPatch(base=base, head=head, path=path)
        change = next((entry for entry in changes if entry.path == path), None)
        if change is None:
            raise ValueError(f"path is not in the round's change list: {path!r}")  # noqa: TRY003  # Names the rejected value.
        key = (base, head, path)
        cached = self._patch_cache.get(key)
        if cached is not None:
            self._patch_cache.move_to_end(key)
            return cached
        # A rename needs both sides in the pathspec for git to pair them
        # into one patch instead of reporting an unrelated delete.
        paths = (path,) if change.renamed_from is None else (change.renamed_from, path)
        output = self._diff_patch(base, head, paths)
        if output is None:
            return DesignPatch(base=base, head=head, path=path, renamed_from=change.renamed_from)
        text, truncated = _truncate_patch(output)
        result = DesignPatch(
            base=base,
            head=head,
            path=path,
            renamed_from=change.renamed_from,
            patch=text,
            truncated=truncated,
        )
        self._patch_cache[key] = result
        while len(self._patch_cache) > _PATCH_CACHE_CAPACITY:
            self._patch_cache.popitem(last=False)
        return result

    def _changed_files(self, base: str, commit: str) -> list[DesignFileChange] | None:
        key = (base, commit)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        output = self._diff(base, commit)
        if output is None:
            return None
        changes = [
            change for change in _parse_name_status(output) if not self._is_framework_change(change)
        ]
        self._cache[key] = changes
        while len(self._cache) > self._capacity:
            self._cache.popitem(last=False)
        return changes

    def _is_framework_change(self, change: DesignFileChange) -> bool:
        """Exclude a change touching framework-owned paths on either side.

        A rename carries two paths, and moving a file out of framework memory
        is framework bookkeeping just as much as moving one in.
        """
        paths = [change.path]
        if change.renamed_from is not None:
            paths.append(change.renamed_from)
        return any(self._is_framework_path(path) for path in paths)

    def _is_framework_path(self, path: str) -> bool:
        if is_project_state_path(path):
            return True
        return any(
            path == prefix or path.startswith(f"{prefix}/") for prefix in self._framework_paths
        )


def _framework_prefixes(workspace: Path) -> tuple[str, ...]:
    """Workspace-relative posix prefixes the framework writes, both layouts."""
    return tuple(
        path.relative_to(workspace).as_posix() for path in framework_memory_paths(workspace)
    )


def _chronological_bases(state: RunView, baseline: str) -> dict[int, str]:
    """Map each round to the newest checkpoint recorded before it.

    This is the fallback base for hypotheses without a recorded parent
    checkpoint (legacy runs): without a revert, a hypothesis's first round
    continues from wherever the run last left the workspace.
    """
    bases: dict[int, str] = {}
    previous = baseline if _COMMIT_PATTERN.fullmatch(baseline) else None
    for record in state.rounds:
        if previous is not None:
            bases[record.round_number] = previous
        commit = _commit(record.commit)
        if commit is not None:
            previous = commit
    return bases


def _commit(value: str | None) -> str | None:
    return value if value is not None and _COMMIT_PATTERN.fullmatch(value) else None


def _parse_name_status(output: str) -> list[DesignFileChange]:
    """Parse NUL-delimited ``--name-status`` records into typed changes."""
    tokens = output.split("\0")
    changes: list[DesignFileChange] = []
    index = 0
    while index < len(tokens):
        status = tokens[index]
        if not status:
            break
        if status.startswith(("R", "C")) and index + _PAIRED_STATUS_FIELDS <= len(tokens):
            renamed_from, path = tokens[index + 1], tokens[index + 2]
            index += _PAIRED_STATUS_FIELDS
            change: Literal["added", "modified", "deleted", "renamed"] = (
                "renamed" if status.startswith("R") else "added"
            )
            if change != "renamed":
                renamed_from = ""
        elif index + 1 < len(tokens):
            path = tokens[index + 1]
            renamed_from = ""
            index += 2
            change = _CHANGE_BY_STATUS.get(status[:1], "modified")
        else:
            break
        changes.append(
            DesignFileChange(path=path, change=change, renamed_from=renamed_from or None)
        )
    return changes


def _text(value: str | None) -> str | None:
    return value or None


def _truncate_patch(output: str) -> tuple[str, bool]:
    """Cut a patch at the size bound, on a line boundary, flagged when cut."""
    if len(output) <= _PATCH_CHAR_LIMIT:
        return output, False
    kept = output[:_PATCH_CHAR_LIMIT]
    newline = kept.rfind("\n")
    return (kept[: newline + 1] if newline >= 0 else kept), True


__all__ = ["DesignLog", "DiffNameStatus", "DiffPatch"]
