"""The nested-repository guard knows the linked worktrees on every Git version (issue #4024).

Before a command reads the work tree, the guard asks ``git worktree list --porcelain``
which linked worktrees the repository has, and allows their ``.git``. NUL-terminated
output (``-z``) arrived in Git 2.36. Older Git, such as the 2.34 that Ubuntu 22.04 ships,
rejects ``-z`` with a usage error (status 129) and prints the same records
newline-terminated without it.

Git is scripted here (``FakeProcesses``) as either version: it lists the worktrees the
test registered, in the format that version prints. On both, a registered worktree is
never a nested repository, a ``.git`` no list explains still is, and a list Git cannot
give fails the command as a Git failure the fault sink hears about, not as a nested
repository.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from vs_project.api import CliGitRepository, Git, GitCommandError, NestedRepositoryError
from vs_sim.api.testing import FakeProcesses, ManualClock, ProcessScript

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_sim.api import ProcessSpec


class _GitVersion(StrEnum):
    """How a Git version answers ``git worktree list --porcelain``."""

    BEFORE_2_36 = "before 2.36"
    """Rejects ``-z``; prints newline-terminated records without it."""
    SINCE_2_36 = "2.36 or later"
    """Prints NUL-terminated records with ``-z`` and newline-terminated ones without."""


_UNKNOWN_SWITCH = b"error: unknown switch `z'\nusage: git worktree list [<options>]\n"
_NOT_A_REPOSITORY = b"fatal: not a git repository (or any of the parent directories): .git\n"
_HEAD = "0123456789abcdef0123456789abcdef01234567"

_STATE = ".vibesys/state"
"""Where linked worktrees go: inside the project, as run worktrees do."""

_ISSUE_LAYOUT = (f"{_STATE}/local/runs/r1/worktrees/s1/workspace",)
"""The run worktree the guard refused on Git 2.34 (issue #4024)."""

_STRAY = ("src/.git", "src/vendor/lib/.git")
"""Repositories outside ``_STATE`` that no worktree list explains: always nested."""


@dataclass(frozen=True)
class _ScriptedGit:
    """Git of one ``version`` whose worktree list names ``linked`` (relative to ``root``)."""

    root: Path
    version: _GitVersion
    linked: tuple[str, ...]
    lists: bool = True
    """Whether ``git worktree list`` succeeds at all."""

    def script(self, spec: ProcessSpec) -> ProcessScript:
        args = tuple(spec.argv[1:])
        if args[:2] != ("worktree", "list"):
            return ProcessScript()  # the index and the guarded command: nothing to report
        nul_terminated = "-z" in args
        if nul_terminated and self.version is _GitVersion.BEFORE_2_36:
            return ProcessScript(returncode=129, stderr=_UNKNOWN_SWITCH)
        if not self.lists:
            return ProcessScript(returncode=128, stderr=_NOT_A_REPOSITORY)
        end = "\0" if nul_terminated else "\n"
        records = [
            (self.root, "branch refs/heads/main"),
            *((self.root / path, "detached") for path in self.linked),
            (self.root.parent / "elsewhere", "detached"),  # a worktree outside the project
        ]
        listing = "".join(
            f"worktree {path}{end}HEAD {_HEAD}{end}{state}{end}{end}" for path, state in records
        )
        return ProcessScript(stdout=listing.encode())


class _RecordingFaults:
    """A ``GitFaultSink`` that keeps every warning."""

    def __init__(self) -> None:
        self.warnings: list[tuple[str, str | None]] = []

    def warning(self, summary: str, *, detail: str | None = None) -> None:
        self.warnings.append((summary, detail))


_GUARDED: dict[str, Callable[[CliGitRepository], object]] = {
    "uncommitted_paths": lambda repo: repo.uncommitted_paths(["."]),
    "tracked_changes_since_head": lambda repo: repo.tracked_changes_since_head(["."]),
    "changed_since": lambda repo: repo.changed_since("HEAD", ["."]),
    "stage_all": lambda repo: repo.stage_all(["."]),
    "commit --only": lambda repo: repo.commit("snapshot", only=["."]),
}
"""Operations that make Git read the work tree, so run the nested-repository guard first."""


def _lay_out(root: Path, linked: tuple[str, ...], stray: tuple[str, ...]) -> None:
    """Make ``root`` a repository with ``linked`` worktrees and ``stray`` repositories."""
    (root / ".git").mkdir(parents=True)
    for path in linked:
        worktree = root / path
        worktree.mkdir(parents=True, exist_ok=True)
        (worktree / ".git").write_text(f"gitdir: {root}/.git/worktrees/{worktree.name}\n")
    for path in stray:
        (root / path).mkdir(parents=True)


def _repository(git: _ScriptedGit, faults: _RecordingFaults) -> CliGitRepository:
    processes = FakeProcesses(git.script, pass_time=ManualClock().advance)
    return CliGitRepository(git.root, git=Git(processes), faults=faults)


_SEGMENT = st.from_regex(r"[A-Za-z0-9 ._-]{1,6}", fullmatch=True).filter(
    lambda name: name not in {".", "..", ".git"}
)
_LINKED = st.lists(
    st.lists(_SEGMENT, min_size=1, max_size=3).map(lambda parts: "/".join([_STATE, *parts])),
    max_size=3,
    unique=True,
)


@pytest.mark.regression
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    version=st.sampled_from(_GitVersion),
    linked=_LINKED.map(tuple),
    stray=st.sampled_from([(), _STRAY]),
    operation=st.sampled_from(sorted(_GUARDED)),
)
@example(
    version=_GitVersion.BEFORE_2_36, linked=_ISSUE_LAYOUT, stray=(), operation="uncommitted_paths"
)
@example(version=_GitVersion.BEFORE_2_36, linked=_ISSUE_LAYOUT, stray=_STRAY, operation="stage_all")
def test_linked_worktrees_are_never_nested_repositories_on_any_git_version(
    tmp_path_factory: pytest.TempPathFactory,
    version: _GitVersion,
    linked: tuple[str, ...],
    stray: tuple[str, ...],
    operation: str,
) -> None:
    """Regression for #4024: Git before 2.36 made every run worktree a nested repository."""
    root = tmp_path_factory.mktemp("project")
    _lay_out(root, linked, stray)
    faults = _RecordingFaults()
    repo = _repository(_ScriptedGit(root, version, linked), faults)

    if stray:
        with pytest.raises(NestedRepositoryError) as refused:
            _GUARDED[operation](repo)
        assert refused.value.paths == stray
    else:
        _GUARDED[operation](repo)
    assert faults.warnings == []


@pytest.mark.regression
@pytest.mark.parametrize("version", list(_GitVersion), ids=lambda version: version.name)
def test_a_worktree_list_git_cannot_give_fails_as_git_not_as_a_nested_repository(
    tmp_path: Path, version: _GitVersion
) -> None:
    """Regression for #4024: a failed listing read as "no linked worktrees" and refused them."""
    root = tmp_path / "project"
    _lay_out(root, _ISSUE_LAYOUT, ())
    faults = _RecordingFaults()
    git = _ScriptedGit(root, version, _ISSUE_LAYOUT, lists=False)
    stderr = _NOT_A_REPOSITORY.decode().strip()

    with pytest.raises(GitCommandError) as failed:
        _repository(git, faults).uncommitted_paths(["."])

    assert (failed.value.command[:3], failed.value.returncode) == (("git", "worktree", "list"), 128)
    assert failed.value.stderr == stderr
    [(summary, detail)] = faults.warnings
    assert "git worktree list" in summary
    assert detail is not None
    assert stderr in detail
