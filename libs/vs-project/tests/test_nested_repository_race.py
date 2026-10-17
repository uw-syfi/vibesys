"""The nested-repository guard holds while linked worktrees come and go (issue #3088).

Before a command reads the work tree, the guard lists the repository's linked worktrees
and then walks the tree for a ``.git``. VibeSys adds and removes candidate worktrees
inside the project (under ``.vibesys/state``) from other threads, so one can appear or
vanish between the listing and the walk.

Git is scripted here (``FakeProcesses``): its ``git worktree list`` reports the worktrees
a small model registered, and the model moves each candidate through Git's own steps at
an exact point, just before or just after a listing. ``git worktree add`` registers the
worktree before it writes the worktree's ``.git``; ``git worktree remove`` deletes the
work tree before it unregisters it. No timing is involved.

A worktree Git registered is never a nested repository, whenever it was added or
removed; a ``.git`` Git does not know about still is.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from vs_project.api import CliGitRepository, Git, NestedRepositoryError, NullGitTrackerEvents
from vs_sim.api.testing import FakeProcesses, ManualClock, ProcessScript

if TYPE_CHECKING:
    from pathlib import Path

    from vs_sim.api import ProcessSpec


class _Step(StrEnum):
    """One of the steps Git takes to add or remove a linked worktree, in Git's order."""

    REGISTER = "register"
    WRITE_DOT_GIT = "write .git"
    DELETE_TREE = "delete tree"
    UNREGISTER = "unregister"


_ADD = (_Step.REGISTER, _Step.WRITE_DOT_GIT)
_REMOVE = (_Step.DELETE_TREE, _Step.UNREGISTER)

_AFTER_FIRST_LIST, _BEFORE_SECOND_LIST, _AFTER_SECOND_LIST, _NEVER = range(4)
"""When a step happens. The guard lists twice at most, so a later step never happens."""

_CANDIDATES = (
    "state/runs/r1/worktrees/s1/workspace",
    "state/runs/r1/worktrees/s2/workspace",
)
"""Where candidate worktrees go: inside the project, as ``.vibesys/state`` is."""

_STRAY = ("src/.git", "state/loose/.git")
"""Repositories no worktree list explains: always nested."""


@dataclass(frozen=True)
class _Lifecycle:
    """A candidate that exists from the start or not, and the steps it then takes."""

    starts_registered: bool
    steps: tuple[tuple[_Step, int], ...]


@dataclass
class _ChurningWorktrees:
    """Scripted Git whose worktree list changes, step by step, around each listing."""

    root: Path
    lifecycles: dict[str, _Lifecycle]
    registered: set[str] = field(default_factory=set)
    lists: int = 0

    def __post_init__(self) -> None:
        for candidate, lifecycle in self.lifecycles.items():
            if lifecycle.starts_registered:
                self._take(candidate, _Step.REGISTER)
                self._take(candidate, _Step.WRITE_DOT_GIT)

    def script(self, spec: ProcessSpec) -> ProcessScript:
        if tuple(spec.argv[1:3]) != ("worktree", "list"):
            return ProcessScript()  # the index and the guarded command: nothing to report
        self.lists += 1
        if self.lists == 2:  # the second listing
            self._churn(_BEFORE_SECOND_LIST)
        records = [self.root, *(self.root / path for path in sorted(self.registered))]
        listing = "".join(f"worktree {path}\0\0" for path in records).encode()
        self._churn(_AFTER_FIRST_LIST if self.lists == 1 else _AFTER_SECOND_LIST)
        return ProcessScript(stdout=listing)

    def _churn(self, point: int) -> None:
        for candidate, lifecycle in self.lifecycles.items():
            for step, when in lifecycle.steps:
                if when == point:
                    self._take(candidate, step)

    def _take(self, candidate: str, step: _Step) -> None:
        tree = self.root / candidate
        match step:
            case _Step.REGISTER:
                self.registered.add(candidate)
            case _Step.WRITE_DOT_GIT:
                tree.mkdir(parents=True, exist_ok=True)
                (tree / ".git").write_text(f"gitdir: {self.root}/.git/worktrees/{tree.name}\n")
            case _Step.DELETE_TREE:
                shutil.rmtree(tree, ignore_errors=True)
            case _Step.UNREGISTER:
                self.registered.discard(candidate)


@st.composite
def _lifecycles(draw: st.DrawFn) -> _Lifecycle:
    starts_registered = draw(st.booleans())
    steps = _REMOVE if starts_registered else draw(st.sampled_from([_ADD, _ADD + _REMOVE]))
    points = draw(
        st.lists(st.integers(_AFTER_FIRST_LIST, _NEVER), min_size=len(steps), max_size=len(steps))
    )
    return _Lifecycle(
        starts_registered=starts_registered, steps=tuple(zip(steps, sorted(points), strict=True))
    )


_ADDED_AFTER_THE_LISTING = _Lifecycle(
    starts_registered=False,
    steps=((_Step.REGISTER, _AFTER_FIRST_LIST), (_Step.WRITE_DOT_GIT, _AFTER_FIRST_LIST)),
)
_ADDED_THEN_REMOVED_DURING_THE_WALK = _Lifecycle(
    starts_registered=False,
    steps=(
        (_Step.REGISTER, _AFTER_FIRST_LIST),
        (_Step.WRITE_DOT_GIT, _AFTER_FIRST_LIST),
        (_Step.DELETE_TREE, _BEFORE_SECOND_LIST),
        (_Step.UNREGISTER, _BEFORE_SECOND_LIST),
    ),
)
_REMOVED_AFTER_THE_LISTING = _Lifecycle(
    starts_registered=True,
    steps=((_Step.DELETE_TREE, _AFTER_FIRST_LIST), (_Step.UNREGISTER, _AFTER_FIRST_LIST)),
)


@pytest.mark.regression
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    lifecycles=st.lists(_lifecycles(), min_size=1, max_size=len(_CANDIDATES)),
    stray=st.sampled_from([(), _STRAY]),
)
@example(lifecycles=[_ADDED_AFTER_THE_LISTING], stray=())
@example(lifecycles=[_ADDED_THEN_REMOVED_DURING_THE_WALK], stray=())
@example(lifecycles=[_REMOVED_AFTER_THE_LISTING], stray=())
@example(lifecycles=[_ADDED_AFTER_THE_LISTING, _REMOVED_AFTER_THE_LISTING], stray=())
@example(lifecycles=[_ADDED_AFTER_THE_LISTING], stray=_STRAY)
def test_worktree_churn_during_the_check_never_makes_a_nested_repository(
    tmp_path_factory: pytest.TempPathFactory, lifecycles: list[_Lifecycle], stray: tuple[str, ...]
) -> None:
    """Regression for #3088: only a ``.git`` no worktree list explains is refused."""
    root = tmp_path_factory.mktemp("project")
    (root / ".git").mkdir()
    for path in stray:
        (root / path).mkdir(parents=True)
    git = _ChurningWorktrees(root, dict(zip(_CANDIDATES, lifecycles, strict=False)))
    processes = FakeProcesses(git.script, pass_time=ManualClock().advance)
    repo = CliGitRepository(root, git=Git(processes), faults=NullGitTrackerEvents())

    if not stray:
        assert repo.uncommitted_paths(["."]) == ()
        return
    with pytest.raises(NestedRepositoryError) as refused:
        repo.uncommitted_paths(["."])
    assert refused.value.paths == stray
